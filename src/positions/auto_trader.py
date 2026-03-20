"""Auto trader — scans for opportunities and opens paper packages autonomously.

Focuses on:
- Near-expiry prediction markets (crypto especially)
- Cross-platform arbitrage spreads
- ITM/OTM analysis for expiring contracts

Respects position limits and only trades in paper mode.
"""
import asyncio
import logging
import time
from datetime import datetime, date, timedelta

try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger("positions.auto_trader")

# Position limits
MAX_TRADE_SIZE = 200.0       # Max $200 per trade
MIN_TRADE_SIZE = 5.0         # Min $5 per trade (supports small live accounts)
MAX_CONCURRENT = 7           # Max 7 open packages (reserve 3 slots for news-driven trades)
MAX_TOTAL_EXPOSURE = 1400.0  # Max $1400 for auto trader (reserve $600 for news)
PORTFOLIO_EXPOSURE_CAP = 0.40  # Kelly portfolio rule: never exceed 40% of total bankroll
TOTAL_BANKROLL = 2000.0      # Total bankroll (auto_trader $1400 + news $600)
SCAN_INTERVAL = 300          # 5 minutes between self-initiated scans (safety net)
MIN_SPREAD_PCT = 12.0        # Minimum 12% spread to ensure profit after fees
# Polymarket: 0% maker fee on limit orders. Use limit orders (maker) to enter,
# taker to exit in worst case = ~2% one-way exit fee.
# Conservative estimate: 0% entry + 2% exit = 2% round-trip
# With 12% min spread - 2% fees = 10% net margin minimum
# Raised from 8%: 21 trades in 12h was excessive churn. 12% min spread
# provides 10% net margin after fees, reducing low-quality entries.
ROUND_TRIP_FEE_PCT = 2.0     # Estimated round-trip fees with limit order entry
MAX_LOSSES_PER_MARKET = 2    # Block market after 2 losses (prevents BTC-top-performer pattern: 6 entries, $24 lost)
MAX_NEW_TRADES_PER_DAY = 3          # Max new positions per calendar day
MARKET_COOLDOWN_SECONDS = 172800    # 48h cooldown per market (was 86400)
MIN_HOURS_TO_EXPIRY = 1.0  # Skip markets expiring within 1 hour (dynamic fees, bot dominance)


class AutoTrader:
    """Autonomous paper trader that creates packages from scanner opportunities."""

    def __init__(self, position_manager, scanner=None, insider_tracker=None,
                 interval: float = SCAN_INTERVAL, decision_logger=None):
        self.pm = position_manager
        self.scanner = scanner
        self.insider_tracker = insider_tracker
        self.interval = interval
        self.dlog = decision_logger
        self._task = None
        self._running = False
        self._trades_opened = 0
        self._trades_skipped = 0
        self._last_trade_time = 0.0
        self._daily_trade_count = 0
        self._daily_trade_date = ""
        self._scan_event = asyncio.Event()  # Fired by arb scanner after each scan
        # News scanner integration — thread-safe queue
        self._news_lock = asyncio.Lock()
        self._news_opportunities: list[dict] = []
        self._political_analyzer = None

    def set_political_analyzer(self, analyzer):
        """Set the political analyzer reference for opportunity consumption."""
        self._political_analyzer = analyzer

    async def add_news_opportunity(self, opp: dict):
        """Called by NewsScanner to queue a normal-urgency signal."""
        async with self._news_lock:
            self._news_opportunities.append(opp)

    async def _drain_news_opportunities(self) -> list[dict]:
        """Drain queued news opportunities for this scan cycle."""
        async with self._news_lock:
            opps = list(self._news_opportunities)
            self._news_opportunities.clear()
            return opps

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info("Auto trader started (interval=%.0fs, max_exposure=$%.0f)", self.interval, MAX_TOTAL_EXPOSURE)

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Auto trader stopped (opened=%d, skipped=%d)", self._trades_opened, self._trades_skipped)

    async def notify_scan_complete(self):
        """Called by arb scanner after each 60s scan — wakes up the trader immediately."""
        self._scan_event.set()

    async def _loop(self):
        await asyncio.sleep(10)  # Let server fully start
        while self._running:
            try:
                await self._scan_and_trade()
            except Exception as e:
                logger.error("Auto trader scan error: %s", e)
            # Wait for EITHER the arb scanner to notify us OR the safety-net timeout.
            # This means we react within seconds of a scan finding opportunities,
            # instead of waiting up to 5 minutes.
            self._scan_event.clear()
            try:
                await asyncio.wait_for(self._scan_event.wait(), timeout=self.interval)
                logger.debug("Auto trader: woken by arb scanner notification")
            except asyncio.TimeoutError:
                logger.debug("Auto trader: safety-net timeout, running scheduled scan")

    def _get_open_market_ids(self, open_pkgs: list[dict]) -> set[str]:
        """Get condition IDs of markets we already have open positions on."""
        ids = set()
        for pkg in open_pkgs:
            for leg in pkg.get("legs", []):
                if leg.get("status") == "open":
                    asset_id = leg.get("asset_id", "")
                    # asset_id format: "{conditionId}:YES" or "{conditionId}:NO"
                    condition_id = asset_id.split(":")[0] if ":" in asset_id else asset_id
                    if condition_id:
                        ids.add(condition_id)
        return ids

    def _check_daily_limit(self) -> bool:
        """Returns True if we can still open trades today."""
        today = date.today().isoformat()
        if self._daily_trade_date != today:
            self._daily_trade_count = 0
            self._daily_trade_date = today
        return self._daily_trade_count < MAX_NEW_TRADES_PER_DAY

    async def _scan_and_trade(self):
        """One scan cycle: find opportunities, filter, create packages."""
        open_pkgs = self.pm.list_packages("open")
        if len(open_pkgs) >= MAX_CONCURRENT:
            logger.info("Auto trader: at max concurrent positions (%d), skipping", len(open_pkgs))
            if self.dlog:
                self.dlog.log_scan_skip("max_concurrent", open_positions=len(open_pkgs))
            return

        total_exposure = sum(p.get("total_cost", 0) for p in open_pkgs)
        if total_exposure >= MAX_TOTAL_EXPOSURE:
            logger.info("Auto trader: at max exposure ($%.2f), skipping", total_exposure)
            if self.dlog:
                self.dlog.log_scan_skip("max_exposure", exposure=round(total_exposure, 2))
            return

        # Kelly portfolio rule: total exposure across ALL positions must not exceed
        # 40% of bankroll. This prevents over-concentration even when individual
        # Kelly fractions are correct. (Research: reduces 80% drawdown probability
        # from 1-in-5 to 1-in-213 at 30% Kelly, we use 40% as generous cap.)
        kelly_cap = TOTAL_BANKROLL * PORTFOLIO_EXPOSURE_CAP
        if total_exposure >= kelly_cap:
            logger.info("Auto trader: at Kelly portfolio cap ($%.2f / $%.2f), skipping",
                        total_exposure, kelly_cap)
            if self.dlog:
                self.dlog.log_scan_skip("kelly_portfolio_cap", exposure=round(total_exposure, 2))
            return

        if not self._check_daily_limit():
            logger.info("Auto trader: daily trade limit (%d/%d), skipping",
                        self._daily_trade_count, MAX_NEW_TRADES_PER_DAY)
            if self.dlog:
                self.dlog.log_scan_skip("daily_limit", trades_today=self._daily_trade_count)
            return

        remaining_budget = min(MAX_TOTAL_EXPOSURE - total_exposure, kelly_cap - total_exposure)
        remaining_slots = MAX_CONCURRENT - len(open_pkgs)
        open_market_ids = self._get_open_market_ids(open_pkgs)

        # Read opportunities from the arb scanner's cache (already scanned every 60s).
        # No need to trigger another scan — the _auto_scan_loop handles that.
        # This lets us evaluate and execute within seconds of data arriving.
        opportunities = []
        if self.scanner:
            try:
                # Use cached results first (fast — no network calls)
                arb_opps = self.scanner.get_opportunities()
                all_events = self.scanner.get_events()

                # If cache is empty (first run or scanner hasn't scanned yet), do one scan
                if not arb_opps and not all_events:
                    result = await self.scanner.scan()
                    logger.info("Auto trader: initial scan fetched %d events, %d opportunities",
                                result.get("events_count", 0), result.get("opportunities_count", 0))
                    arb_opps = self.scanner.get_opportunities()
                    all_events = self.scanner.get_events()
                else:
                    logger.info("Auto trader: using cached scanner data (%d arb opps, %d events)",
                                len(arb_opps), len(all_events))

                # 1. Cross-platform arbitrage opportunities (highest priority)
                for arb in arb_opps:
                    opp = self._arb_to_opportunity(arb)
                    if opp:
                        opp["_score"] = opp.get("profit_pct", 0) * 3.0  # Arb premium
                        opportunities.append(opp)

                # 2. Single-platform directional bets from ALL platform events
                platform_opps = self._events_to_opportunities(all_events)
                opportunities.extend(platform_opps)

            except Exception as e:
                logger.warning("Auto trader: scanner failed: %s", e)

        if not opportunities:
            # Fallback: direct Polymarket scan (only if scanner failed entirely)
            opportunities = await self._scan_polymarket()

        # Merge queued news opportunities with score boost
        news_opps = await self._drain_news_opportunities()
        for news_opp in news_opps:
            news_opp["_score"] = news_opp.get("_score", 10.0) * 2.0  # News edge boost
            opportunities.append(news_opp)
        if news_opps:
            logger.info("Auto trader: merged %d news opportunities", len(news_opps))

        # Merge multi-outcome arbitrage opportunities
        if self.scanner:
            try:
                multi_opps = await self.scanner.scan_multi_outcome()
                for mo in multi_opps:
                    # Multi-outcome arb is guaranteed profit — high priority
                    mo["_score"] = mo.get("profit_pct", 0) * 5.0  # 5x arb premium
                    opportunities.append(mo)
                if multi_opps:
                    logger.info("Auto trader: merged %d multi-outcome arb opportunities", len(multi_opps))
            except Exception as e:
                logger.warning("Auto trader: multi-outcome scan failed: %s", e)

        # Merge political synthetic opportunities
        if self._political_analyzer:
            political_opps = self._political_analyzer.get_opportunities()
            for pol_opp in political_opps:
                ev_pct = pol_opp.get("net_expected_value_pct", 0)
                confidence = pol_opp.get("strategy", {}).get("confidence", "medium")
                conf_mult = {"high": 1.5, "medium": 1.0}.get(confidence, 0.5)
                cross_platform = len(set(pol_opp.get("platforms", []))) > 1
                platform_mult = 1.5 if cross_platform else 1.0
                pol_opp["_score"] = ev_pct * conf_mult * platform_mult
                pol_opp["profit_pct"] = ev_pct
                opportunities.append(pol_opp)
            if political_opps:
                logger.info("Auto trader: merged %d political opportunities", len(political_opps))

        if not opportunities:
            logger.info("Auto trader: no opportunities found this cycle")
            return

        logger.info("Auto trader: found %d opportunities, budget=$%.2f, slots=%d",
                     len(opportunities), remaining_budget, remaining_slots)
        if self.dlog:
            self.dlog.log_scan_start(len(open_pkgs), total_exposure, remaining_budget, remaining_slots)

        trades_this_cycle = 0
        for opp in opportunities:
            opp_title = (opp.get("title") or opp.get("canonical_title") or "?")[:100]
            if trades_this_cycle >= remaining_slots:
                break
            if remaining_budget < MIN_TRADE_SIZE:
                break

            # Filter: skip zero-price markets (no liquidity, phantom opportunities)
            buy_yes_price = opp.get("buy_yes_price", 0)
            buy_no_price = opp.get("buy_no_price", 0)
            if buy_yes_price < 0.01 and buy_no_price < 0.01:
                self._trades_skipped += 1
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "zero_price")
                continue

            # Filter: require minimum spread (use net profit after fees when available)
            spread_pct = opp.get("net_profit_pct") or opp.get("profit_pct", 0)
            if spread_pct < MIN_SPREAD_PCT:
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "low_spread", spread_pct=spread_pct)
                continue

            # Skip markets we already have positions on
            market_id = opp.get("buy_yes_market_id", "")
            if market_id and market_id in open_market_ids:
                self._trades_skipped += 1
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "already_open")
                continue

            # Prioritize crypto-related and near-expiry
            title = (opp.get("title") or opp.get("canonical_title") or "").lower()
            is_crypto = any(kw in title for kw in ["btc", "bitcoin", "eth", "ethereum", "crypto", "solana", "sol", "xrp"])

            # Check expiry — parse with time precision when available
            expiry = opp.get("expiry") or opp.get("end_date") or ""
            is_near_expiry = False
            days_to_expiry = 999
            hours_to_expiry = float('inf')
            if expiry:
                try:
                    exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                    hours_to_expiry = max(0, (exp_dt - datetime.now(exp_dt.tzinfo)).total_seconds() / 3600)
                    days_to_expiry = hours_to_expiry / 24
                except (ValueError, TypeError):
                    try:
                        exp_date = datetime.strptime(expiry[:10], "%Y-%m-%d").date()
                        days_to_expiry = (exp_date - date.today()).days
                        hours_to_expiry = days_to_expiry * 24
                    except (ValueError, TypeError):
                        pass
                is_near_expiry = 2 < days_to_expiry <= 30

            # Skip short-duration markets (15-min, 1-hour crypto)
            # Research: dynamic fees up to 3.15%, 73% of arb captured by sub-100ms bots
            if hours_to_expiry < MIN_HOURS_TO_EXPIRY:
                self._trades_skipped += 1
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "short_duration",
                                                   hours=round(hours_to_expiry, 1))
                continue

            # Skip markets expiring within 2 days
            if days_to_expiry <= 2:
                self._trades_skipped += 1
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "too_near_expiry", days=days_to_expiry)
                continue

            # Score: crypto near-expiry > crypto > near-expiry > other
            score = spread_pct
            if is_crypto:
                score *= 2.0
            if is_near_expiry:
                score *= 1.5

            # Favorite-longshot bias (research-validated edge):
            # - Contracts >$0.80: favorites win MORE than implied → boost
            # - Contracts $0.15-$0.30: longshots lose MORE than implied → penalize
            # - On Kalshi, buyers of contracts <$0.10 lose >60% of their money
            favored = min(buy_yes_price, buy_no_price) if buy_no_price > 0 else buy_yes_price
            if favored >= 0.80:
                score *= 1.8  # Strong favorite — historically wins more than price implies
            elif favored >= 0.70:
                score *= 1.4  # Moderate favorite
            elif favored <= 0.20:
                score *= 0.4  # Longshot penalty — these lose far more than implied
            elif favored <= 0.30:
                score *= 0.7  # Mild longshot penalty

            # Insider signal boost: if whales/insiders have positions, boost score
            insider_signal = None
            market_id = opp.get("buy_yes_market_id", "")
            if self.insider_tracker and market_id:
                insider_signal = self.insider_tracker.get_insider_signal(market_id)
                if insider_signal and insider_signal.get("has_signal"):
                    strength = insider_signal.get("signal_strength", 0)
                    # Strong insider signal = 2-3x score boost
                    score *= (1.0 + strength * 2.0)
                    if insider_signal.get("suspicious_count", 0) > 0:
                        score *= 1.5  # Extra boost for suspicious insiders
                    opp["insider_signal"] = insider_signal

            # Skip low-score opportunities
            if score < MIN_SPREAD_PCT:
                self._trades_skipped += 1
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "low_score", score=round(score, 1),
                                                   spread_pct=spread_pct, days_to_expiry=days_to_expiry)
                continue

            # Size the trade
            trade_size = min(MAX_TRADE_SIZE, remaining_budget / 2, remaining_budget)
            trade_size = max(MIN_TRADE_SIZE, trade_size)

            # Extract market details from opportunity
            buy_yes_platform = opp.get("buy_yes_platform", "polymarket")
            buy_yes_price = opp.get("buy_yes_price", 0.5)
            buy_no_platform = opp.get("buy_no_platform", "polymarket")
            buy_no_price = opp.get("buy_no_price", 0.5)
            yes_market_id = opp.get("buy_yes_market_id", "")
            no_market_id = opp.get("buy_no_market_id", "")

            if not yes_market_id or not no_market_id:
                # Try to extract from matched event
                matched = opp.get("matched_event", {})
                markets = matched.get("markets", [])
                for m in markets:
                    if m.get("platform") == buy_yes_platform and not yes_market_id:
                        yes_market_id = m.get("market_id", m.get("id", ""))
                    if m.get("platform") == buy_no_platform and not no_market_id:
                        no_market_id = m.get("market_id", m.get("id", ""))

            if not yes_market_id and not no_market_id:
                self._trades_skipped += 1
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "no_market_id")
                continue

            # Skip legs at price ceiling (>= 0.95) — no upside
            if buy_yes_price >= 0.95:
                yes_market_id = ""  # don't create YES leg
            if buy_no_price >= 0.95:
                no_market_id = ""  # don't create NO leg
            if not yes_market_id and not no_market_id:
                self._trades_skipped += 1
                continue

            # Create the package — DIRECTIONAL BET on one side only
            # Buying both YES and NO on the same market locks in the spread minus fees = guaranteed loss
            # Instead: pick the side with better EXPECTED VALUE
            from .position_manager import create_package, create_leg, create_exit_rule

            trade_title = opp.get("title") or opp.get("canonical_title") or f"Auto-{int(time.time())}"

            # Cooldown: don't re-enter a market within 24 hours of exiting it
            # Track BOTH by condition ID and normalized title to catch duplicates
            # Also block markets with 2+ historical losses (prevents churning)
            recently_closed_ids = set()
            recently_closed_titles = set()
            market_loss_counts = {}  # title → loss count (all time)
            market_exit_prices = {}  # title → last exit price (for price-change requirement)
            for p in self.pm.list_packages("closed"):
                ptitle = (p.get("name", "").replace("Auto: ", "").replace("News: ", "").lower().strip())[:50]
                # Track all-time loss count per market
                pnl = p.get("realized_pnl", p.get("unrealized_pnl", 0))
                if ptitle and pnl < 0:
                    market_loss_counts[ptitle] = market_loss_counts.get(ptitle, 0) + 1
                    # Record the exit price of the last losing trade for price-change check
                    for leg in p.get("legs", []):
                        if leg.get("current_price", 0) > 0:
                            market_exit_prices[ptitle] = leg["current_price"]
                # 24-hour cooldown window (was 4 hours — too short, NCAA entered 5 times)
                if time.time() - p.get("updated_at", 0) < MARKET_COOLDOWN_SECONDS:  # 48 hours
                    for leg in p.get("legs", []):
                        cid = leg.get("asset_id", "").split(":")[0]
                        if cid:
                            recently_closed_ids.add(cid)
                    if ptitle:
                        recently_closed_titles.add(ptitle)

            if yes_market_id in recently_closed_ids or no_market_id in recently_closed_ids:
                self._trades_skipped += 1
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "cooldown_after_exit")
                continue

            # Title-based duplicate check: don't re-enter the same event by title
            norm_title = trade_title.lower().strip()[:50]
            if norm_title in recently_closed_titles:
                self._trades_skipped += 1
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "cooldown_title_match")
                continue

            # Block markets with too many historical losses
            if market_loss_counts.get(norm_title, 0) >= MAX_LOSSES_PER_MARKET:
                self._trades_skipped += 1
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, f"max_losses_reached ({market_loss_counts[norm_title]} losses)")
                continue

            # Price-change requirement: after a loss, require 10% favorable move
            # before re-entering the same market (research: prevents re-entering
            # unchanged losing markets just because cooldown expired)
            if norm_title in market_exit_prices:
                exit_price = market_exit_prices[norm_title]
                current_entry = buy_yes_price if buy_yes_price > 0 else buy_no_price
                if current_entry > 0 and exit_price > 0:
                    price_change = abs(current_entry - exit_price) / exit_price
                    if price_change < 0.10:  # Less than 10% price change
                        self._trades_skipped += 1
                        if self.dlog:
                            self.dlog.log_opportunity_skip(
                                opp_title,
                                f"insufficient_price_change ({price_change:.1%} < 10% since last loss exit)")
                        continue

            # Also check open positions by title — don't open duplicates of existing positions
            open_titles = set()
            for p in self.pm.list_packages("open"):
                ptitle = (p.get("name", "").replace("Auto: ", "").replace("News: ", "").lower().strip())[:50]
                if ptitle:
                    open_titles.add(ptitle)
            if norm_title in open_titles:
                self._trades_skipped += 1
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "duplicate_open_position")
                continue

            # Multi-outcome arbitrage: buy all outcomes when sum < $1.00
            if opp.get("opportunity_type") == "multi_outcome_arb":
                try:
                    pkg = create_package(f"Auto: {trade_title[:60]}", "multi_outcome_arb")
                except ValueError:
                    continue

                outcomes = opp.get("outcomes", [])
                if not outcomes:
                    continue

                # Allocate proportionally to each outcome's price
                total_price = sum(o.get("yes_price", 0) for o in outcomes)
                if total_price <= 0:
                    continue

                for outcome in outcomes:
                    leg_price = outcome.get("yes_price", 0)
                    if leg_price <= 0:
                        continue
                    leg_cost = round(trade_size * (leg_price / total_price), 2)
                    leg_cost = max(MIN_TRADE_SIZE, leg_cost)

                    pkg["legs"].append(create_leg(
                        platform="polymarket",
                        leg_type="prediction_yes",
                        asset_id=f"{outcome['condition_id']}:YES",
                        asset_label=f"YES: {outcome.get('title', '?')[:40]}",
                        entry_price=leg_price,
                        cost=leg_cost,
                        expiry=opp.get("expiry", "2026-12-31")[:10],
                    ))

                # Multi-outcome arb: guaranteed profit — only exit on safety
                pkg["exit_rules"].append(create_exit_rule("stop_loss", {"stop_pct": -15}))

                if not pkg["legs"]:
                    self._trades_skipped += 1
                    continue

                pkg["_use_limit_orders"] = True
                pkg_name = pkg.get("name", opp_title)
                try:
                    result = await self.pm.execute_package(pkg)
                    if result.get("success"):
                        trades_this_cycle += 1
                        self._trades_opened += 1
                        self._daily_trade_count += 1
                        remaining_budget -= trade_size
                        logger.info("Auto trader OPENED multi-outcome arb: %s (%d outcomes, spread=%.2f%%)",
                                    pkg_name, len(outcomes), spread_pct)
                        if self.dlog:
                            self.dlog.log_trade_opened(
                                pkg_id=pkg.get("id", ""), title=pkg_name,
                                strategy="multi_outcome_arb",
                                side="ALL_OUTCOMES", price=round(total_price, 4),
                                size=trade_size, score=score, spread_pct=spread_pct,
                                conviction=1.0,  # Guaranteed profit
                                days_to_expiry=days_to_expiry, volume=opp.get("volume", 0))
                except Exception as e:
                    logger.warning("Auto trader: multi-outcome trade failed: %s", e)
                    if self.dlog:
                        self.dlog.log_trade_failed(opp_title, str(e))
                continue

            # Political synthetic: multi-leg with weight-based allocation
            if opp.get("opportunity_type") == "political_synthetic":
                try:
                    pkg = create_package(f"Auto: {trade_title[:60]}", "political_synthetic")
                except ValueError:
                    continue

                opp_legs = opp.get("legs", [])
                if not opp_legs:
                    continue

                for opp_leg in opp_legs:
                    leg_cost = round(trade_size * opp_leg.get("weight", 1.0 / len(opp_legs)), 2)
                    leg_cost = max(MIN_TRADE_SIZE, leg_cost)
                    side = opp_leg.get("side", "YES")
                    leg_type = "prediction_yes" if side == "YES" else "prediction_no"
                    price = opp_leg.get("yes_price", 0.5) if side == "YES" else opp_leg.get("no_price", 0.5)
                    pkg["legs"].append(create_leg(
                        platform=opp_leg.get("platform", "polymarket"),
                        leg_type=leg_type,
                        asset_id=f"{opp_leg['event_id']}:{side}",
                        asset_label=f"{side} @ {opp_leg.get('platform', '?')}: {opp_leg.get('title', '?')[:40]}",
                        entry_price=price if price > 0 else 0.5,
                        cost=leg_cost,
                        expiry=opp.get("expiry", "2026-12-31")[:10],
                    ))

                pkg["exit_rules"].append(create_exit_rule("target_profit", {"target_pct": 50}))
                pkg["exit_rules"].append(create_exit_rule("stop_loss", {"stop_pct": -40}))
                pkg["exit_rules"].append(create_exit_rule("trailing_stop", {"current": 35, "bound_min": 15, "bound_max": 50}))
                pkg["_political_strategy"] = opp.get("strategy", {})

                # Political packages skip normal strategy/side determination.
                # Fall through to the execution block below (try/await pm.execute_package).
                if not pkg["legs"]:
                    self._trades_skipped += 1
                    continue

                pkg["_use_limit_orders"] = True
                pkg_name = pkg.get("name", opp_title)
                bet_side = "POLITICAL"
                bet_conviction = 0.0
                entry_price = 0.5
                try:
                    result = await self.pm.execute_package(pkg)
                    if result.get("success"):
                        trades_this_cycle += 1
                        self._trades_opened += 1
                        self._daily_trade_count += 1
                        remaining_budget -= trade_size
                        logger.info("Auto trader OPENED political: %s (ev=%.1f%%, size=$%.2f)",
                                    pkg_name, spread_pct, trade_size)
                        if self.dlog:
                            self.dlog.log_trade_opened(
                                pkg_id=pkg.get("id", ""), title=pkg_name,
                                strategy="political_synthetic",
                                side=bet_side, price=entry_price,
                                size=trade_size, score=score, spread_pct=spread_pct,
                                conviction=bet_conviction,
                                days_to_expiry=days_to_expiry, volume=opp.get("volume", 0))
                except Exception as e:
                    logger.warning("Auto trader: political trade failed: %s", e)
                    if self.dlog:
                        self.dlog.log_trade_failed(opp_title, str(e))
                continue

            # Determine strategy:
            # - synthetic_derivative: related markets with different price targets
            #   Can be same-platform (e.g., BTC >$90K YES + BTC >$100K NO = bull spread)
            #   or cross-platform. Does NOT require cross-platform.
            # - cross_platform_arb: same market on different platforms (guaranteed spread)
            # - pure_prediction: directional bet on one side
            is_cross_platform = buy_yes_platform != buy_no_platform and yes_market_id and no_market_id
            is_synthetic = opp.get("is_synthetic", False)

            if is_synthetic:
                # Synthetics work on same or different platforms — different strike prices
                # create the edge, not platform differences
                strategy = "synthetic_derivative"
            elif is_cross_platform:
                strategy = "cross_platform_arb"
            else:
                strategy = "pure_prediction"

            try:
                pkg = create_package(f"Auto: {trade_title[:60]}", strategy)
            except ValueError:
                pkg = create_package(f"Auto: {trade_title[:60]}", "pure_prediction")

            if is_cross_platform or is_synthetic:
                # Multi-leg trade: cross-platform arb OR synthetic derivative
                # Both require buying YES on one market/platform and NO on another
                #
                # Cross-platform arb: same event, different platforms, guaranteed spread
                # Synthetic: different strike prices (same or different platform), structural edge
                #
                # Key fix: synthetics no longer require cross-platform — same-platform
                # synthetics are valid (e.g., BTC >$90K YES + BTC >$100K NO = bull spread)

                # Skip if either side has no real price (zero-price markets have no liquidity)
                if buy_yes_price < 0.01 or buy_no_price < 0.01:
                    self._trades_skipped += 1
                    if self.dlog:
                        self.dlog.log_opportunity_skip(opp_title, "zero_price_multi_leg",
                                                       yes_price=round(buy_yes_price, 4),
                                                       no_price=round(buy_no_price, 4))
                    continue

                # Need market IDs for both legs
                if not yes_market_id or not no_market_id:
                    self._trades_skipped += 1
                    if self.dlog:
                        self.dlog.log_opportunity_skip(opp_title, "missing_market_id_multi_leg",
                                                       yes_id=yes_market_id[:12] if yes_market_id else "",
                                                       no_id=no_market_id[:12] if no_market_id else "")
                    continue

                # For synthetics, size by total cost efficiency
                # Lower total cost = higher potential return per dollar
                if is_synthetic:
                    synth = opp.get("synthetic_info", {})
                    total_cost = synth.get("total_cost", buy_yes_price + buy_no_price)
                    # Allocate proportionally to each leg's price
                    if total_cost > 0:
                        yes_alloc = round(trade_size * (buy_yes_price / total_cost), 2)
                        no_alloc = round(trade_size * (buy_no_price / total_cost), 2)
                    else:
                        yes_alloc = no_alloc = round(trade_size / 2, 2)
                    yes_label = f"YES @ {buy_yes_platform} (strike: ${synth.get('yes_target', '?'):,.0f})"
                    no_label = f"NO @ {buy_no_platform} (strike: ${synth.get('no_target', '?'):,.0f})"
                else:
                    yes_alloc = no_alloc = round(trade_size / 2, 2)
                    yes_label = f"YES @ {buy_yes_platform}"
                    no_label = f"NO @ {buy_no_platform}"

                pkg["legs"].append(create_leg(
                    platform=buy_yes_platform, leg_type="prediction_yes",
                    asset_id=f"{yes_market_id}:YES", asset_label=yes_label,
                    entry_price=buy_yes_price,
                    cost=yes_alloc, expiry=expiry[:10] if expiry else "2026-12-31",
                ))
                pkg["legs"].append(create_leg(
                    platform=buy_no_platform, leg_type="prediction_no",
                    asset_id=f"{no_market_id}:NO", asset_label=no_label,
                    entry_price=buy_no_price,
                    cost=no_alloc, expiry=expiry[:10] if expiry else "2026-12-31",
                ))
                if is_synthetic:
                    pkg["_synthetic_info"] = opp.get("synthetic_info", {})
            else:
                # Directional bet: pick ONE side based on EXPECTED VALUE
                # Historical data shows: NO bets at 0.33-0.84 resolving to $1 = all profits
                #                        YES bets at 0.09-0.17 = almost always losers
                #
                # Strategy: bet the side with higher implied probability (the favorite)
                # - If YES >= 0.60 → bet YES (market already thinks this resolves YES)
                # - If YES <= 0.40 → bet NO (market thinks this resolves NO)
                # - If 0.40 < YES < 0.60 → bet the side closer to 0.50 (coin flip = skip low conviction)
                #
                # Skip extreme OTM bets (entry price < 0.15) — these are lottery tickets
                # that lose 85%+ of the time

                if buy_yes_price >= 0.60 and yes_market_id:
                    # Market favors YES → bet YES (riding the consensus)
                    side, side_price, side_id = "YES", buy_yes_price, yes_market_id
                    leg_type = "prediction_yes"
                elif buy_yes_price <= 0.40 and no_market_id:
                    # Market favors NO → bet NO (riding the consensus)
                    side, side_price, side_id = "NO", (1.0 - buy_yes_price), no_market_id
                    leg_type = "prediction_no"
                elif buy_yes_price >= 0.50 and yes_market_id:
                    # Slight YES lean
                    side, side_price, side_id = "YES", buy_yes_price, yes_market_id
                    leg_type = "prediction_yes"
                elif no_market_id:
                    # Slight NO lean
                    side, side_price, side_id = "NO", (1.0 - buy_yes_price), no_market_id
                    leg_type = "prediction_no"
                elif yes_market_id:
                    side, side_price, side_id = "YES", buy_yes_price, yes_market_id
                    leg_type = "prediction_yes"
                else:
                    self._trades_skipped += 1
                    continue

                # Skip extreme OTM (lottery tickets) — entry < 0.15 loses 85%+ of the time
                # Allow high-probability entries (> 0.85) — they resolve at $1.00
                # Only skip truly extreme entries (> 0.96) where fees exceed max profit
                if side_price < 0.15:
                    self._trades_skipped += 1
                    if self.dlog:
                        self.dlog.log_opportunity_skip(opp_title, "extreme_otm",
                                                       side=side, price=round(side_price, 4))
                    continue
                if side_price > 0.96:
                    self._trades_skipped += 1
                    if self.dlog:
                        self.dlog.log_opportunity_skip(opp_title, "extreme_itm",
                                                       side=side, price=round(side_price, 4))
                    continue

                # Quarter Kelly position sizing (research-validated: retains 56% of
                # max growth rate, ~3% chance of halving bankroll)
                #
                # Kelly f* = (b * p_true - (1 - p_true)) / b
                # where b = net odds = (1 - market_price) / market_price
                #       p_true = our edge estimate = market_price + edge_bonus
                #
                # Edge bonus: +5% for favorites (>0.70), +2% base edge assumption
                edge_bonus = 0.02  # Base 2% edge assumption (we select favorable markets)
                if side_price >= 0.70:
                    edge_bonus = 0.05  # Favorite-longshot bias gives us more edge
                elif side_price <= 0.30:
                    edge_bonus = 0.01  # Less confident on longshots

                p_true = min(0.95, side_price + edge_bonus)
                net_odds = (1.0 - side_price) / side_price if side_price > 0 else 1.0
                kelly_full = (net_odds * p_true - (1.0 - p_true)) / net_odds if net_odds > 0 else 0
                kelly_quarter = max(0.0, kelly_full * 0.25)

                # Apply Kelly fraction to remaining budget, capped at MAX_TRADE_SIZE
                sized_trade = round(min(MAX_TRADE_SIZE, remaining_budget * kelly_quarter), 2)
                sized_trade = max(MIN_TRADE_SIZE, min(sized_trade, trade_size))

                pkg["legs"].append(create_leg(
                    platform=buy_yes_platform if side == "YES" else buy_no_platform,
                    leg_type=leg_type,
                    asset_id=f"{side_id}:{side}",
                    asset_label=f"{side} @ {buy_yes_platform if side == 'YES' else buy_no_platform}",
                    entry_price=side_price if side_price > 0 else 0.5,
                    cost=sized_trade,
                    expiry=expiry[:10] if expiry else "2026-12-31",
                ))
                pkg["_bet_side"] = side
                pkg["_entry_conviction"] = round(side_price, 3)
                trade_size = sized_trade  # Update for budget tracking

            if not pkg["legs"]:
                self._trades_skipped += 1
                continue

            # Exit rules — strategy-dependent
            if strategy == "cross_platform_arb":
                # Cross-platform arb: guaranteed profit at resolution — HOLD TO RESOLUTION
                # Fee-aware stop: only exit if loss exceeds net spread + fees
                # The spread is locked in at entry; exiting early forfeits the guarantee
                net_pct = opp.get("net_profit_pct", spread_pct)
                # Stop loss = -(net spread + 5% buffer) — only exit on genuine spread collapse
                arb_stop = round(max(-60, -(net_pct + 5)), 1)
                pkg["exit_rules"].append(create_exit_rule("stop_loss", {"stop_pct": arb_stop}))
                # No trailing stop, no time decay — these resolve at $0 or $1
                # Flag as hold-to-resolution so exit engine skips soft triggers
                pkg["_hold_to_resolution"] = True
                pkg["_min_hold_until"] = time.time() + 86400
            elif strategy == "synthetic_derivative":
                # Synthetics: structural edge from different strike prices — HOLD TO RESOLUTION
                # The payoff depends on where the underlying lands relative to strikes
                # Wide stop only — cutting early destroys the structural edge
                pkg["exit_rules"].append(create_exit_rule("target_profit", {"target_pct": 80}))
                pkg["exit_rules"].append(create_exit_rule("stop_loss", {"stop_pct": -50}))
                # No trailing stop — synthetics need room to breathe
                pkg["_hold_to_resolution"] = True
                pkg["_min_hold_until"] = time.time() + 86400
            else:
                # Pure prediction: directional bet
                # Differentiate by entry price level:
                #   High-probability (> 0.85): hold to resolution, no trailing stop
                #   Mid-range (0.30-0.85): standard trailing stop
                #   Longshots (< 0.30): wide trailing stop
                avg_entry = side_price if not is_cross_platform else 0.5
                if avg_entry > 0.85:
                    # High-probability contracts resolve at $1.00 — hold to resolution
                    # Max upside is only 5-17%, trailing stops destroy these
                    # Target at realistic max (price → $1.00 minus fees)
                    max_profit = round(((1.0 - avg_entry) / avg_entry) * 100, 1)
                    pkg["exit_rules"].append(create_exit_rule("target_profit",
                        {"target_pct": max(5, max_profit - 2)}))
                    # Wide stop only for catastrophic thesis invalidation
                    pkg["exit_rules"].append(create_exit_rule("stop_loss", {"stop_pct": -60}))
                    # No trailing stop — these should resolve, not be scalped
                    pkg["_hold_to_resolution"] = True
                    pkg["_min_hold_until"] = time.time() + 86400
                else:
                    # Standard prediction — tuned from 31-trade analysis
                    pkg["exit_rules"].append(create_exit_rule("target_profit", {"target_pct": 50}))
                    pkg["exit_rules"].append(create_exit_rule("stop_loss", {"stop_pct": -40}))
                    pkg["exit_rules"].append(create_exit_rule("trailing_stop", {"current": 35, "bound_min": 15, "bound_max": 50}))
                    pkg["_min_hold_until"] = time.time() + 86400

            # Use limit orders for 0% maker fees on entry
            pkg["_use_limit_orders"] = True

            # Execute
            pkg_name = pkg.get("name", opp_title)
            bet_side = pkg.get("_bet_side", "SYNTHETIC" if is_synthetic else ("BOTH" if is_cross_platform else "?"))
            bet_conviction = pkg.get("_entry_conviction", round(abs(buy_yes_price - 0.5), 3))
            entry_price = side_price if not is_cross_platform else buy_yes_price
            try:
                result = await self.pm.execute_package(pkg)
                if result.get("success"):
                    trades_this_cycle += 1
                    self._trades_opened += 1
                    self._daily_trade_count += 1
                    remaining_budget -= trade_size
                    if market_id:
                        open_market_ids.add(market_id)
                    logger.info("Auto trader OPENED: %s (spread=%.1f%%, size=$%.2f, score=%.1f)",
                                pkg_name, spread_pct, trade_size, score)
                    if self.dlog:
                        self.dlog.log_trade_opened(
                            pkg_id=pkg.get("id", ""), title=pkg_name,
                            strategy=pkg.get("strategy_type", ""),
                            side=bet_side, price=entry_price,
                            size=trade_size, score=score, spread_pct=spread_pct,
                            conviction=bet_conviction,
                            days_to_expiry=days_to_expiry,
                            volume=opp.get("volume", 0),
                            insider_signal=opp.get("insider_signal"),
                        )
                else:
                    self._trades_skipped += 1
                    logger.warning("Auto trader: execution failed for %s: %s", pkg_name, result.get("error"))
                    if self.dlog:
                        self.dlog.log_trade_failed(pkg_name, result.get("error", "unknown"))
            except Exception as e:
                self._trades_skipped += 1
                logger.error("Auto trader: exception creating package: %s", e)
                if self.dlog:
                    self.dlog.log_trade_failed(pkg_name, str(e))

        if trades_this_cycle > 0:
            logger.info("Auto trader: opened %d new positions this cycle", trades_this_cycle)

    def _arb_to_opportunity(self, arb: dict) -> dict | None:
        """Convert an ArbitrageOpportunity dict to auto_trader opportunity format.

        Fixed: resolves market IDs per-platform correctly, rejects same-platform arb
        (buying YES+NO on same platform = guaranteed loss after fees), and enforces
        minimum spread thresholds per platform pair.
        """
        matched = arb.get("matched_event", {})
        title = matched.get("canonical_title", "")
        if not title:
            return None

        buy_yes_platform = arb.get("buy_yes_platform", "")
        buy_no_platform = arb.get("buy_no_platform", "")

        # CRITICAL FIX: reject same-platform "arb" — buying both YES and NO on
        # the same platform costs ~$1.00 and guarantees a fee-only loss.
        # This was the cause of 29/31 trades being pure_prediction losses.
        if buy_yes_platform == buy_no_platform:
            is_synthetic = arb.get("is_synthetic", False)
            if not is_synthetic:
                # Same-platform, non-synthetic = not real arb, skip
                logger.debug("Rejecting same-platform arb on %s: %s", buy_yes_platform, title[:40])
                return None
            # Same-platform synthetics ARE valid (different strike prices)

        # Resolve market IDs — try multiple ID fields and match ALL markets per platform
        markets = matched.get("markets", [])
        buy_yes_market_id = ""
        buy_no_market_id = ""

        for m in markets:
            platform = m.get("platform", "")
            # Try multiple ID fields — platforms use different naming
            market_id = (m.get("event_id") or m.get("market_id") or
                         m.get("conditionId") or m.get("condition_id") or
                         m.get("id") or "")
            if not market_id:
                continue

            if platform == buy_yes_platform and not buy_yes_market_id:
                buy_yes_market_id = market_id
            if platform == buy_no_platform and not buy_no_market_id:
                buy_no_market_id = market_id

        buy_yes_price = arb.get("buy_yes_price", 0)
        buy_no_price = arb.get("buy_no_price", 0)
        # Skip if either side has no real price (0 or near-0)
        if buy_yes_price < 0.01 or buy_no_price < 0.01:
            return None

        # Enforce per-platform-pair minimum spread thresholds
        # Cross-platform fees: Polymarket taker ~2% + Kalshi ~1.2% = ~3.2% round-trip
        profit_pct = arb.get("profit_pct", 0)
        is_cross_platform = buy_yes_platform != buy_no_platform
        if is_cross_platform:
            min_spread = 3.5  # Must exceed combined cross-platform fees
            if profit_pct < min_spread:
                logger.debug("Cross-platform spread too thin (%.1f%% < %.1f%%): %s",
                             profit_pct, min_spread, title[:40])
                return None

        # Log when we find cross-platform matches but can't execute
        if is_cross_platform and (not buy_yes_market_id or not buy_no_market_id):
            logger.info("Cross-platform arb found but missing market ID: "
                        "yes=%s(%s) no=%s(%s) spread=%.1f%% | %s",
                        buy_yes_platform, buy_yes_market_id[:12] if buy_yes_market_id else "MISSING",
                        buy_no_platform, buy_no_market_id[:12] if buy_no_market_id else "MISSING",
                        profit_pct, title[:40])

        opp = {
            "title": title,
            "canonical_title": title,
            "buy_yes_platform": buy_yes_platform,
            "buy_yes_price": buy_yes_price,
            "buy_no_platform": buy_no_platform,
            "buy_no_price": buy_no_price,
            "buy_yes_market_id": buy_yes_market_id,
            "buy_no_market_id": buy_no_market_id,
            "profit_pct": profit_pct,
            "expiry": matched.get("expiry", ""),
            "volume": arb.get("combined_volume", 0),
            "matched_event": matched,
        }
        # Pass through synthetic derivative info
        if arb.get("is_synthetic"):
            opp["is_synthetic"] = True
            opp["synthetic_info"] = arb.get("synthetic_info", {})
        return opp

    def _events_to_opportunities(self, matched_events: list[dict]) -> list[dict]:
        """Convert matched events from ALL platforms into directional bet opportunities.

        Each matched event may have markets on multiple platforms. We create
        one opportunity per platform market, so Kalshi, Limitless, PredictIt
        events all get evaluated alongside Polymarket ones.

        Only includes platforms that have executors configured (can actually trade).
        """
        tradeable_platforms = set(self.pm.executors.keys()) if self.pm else {"polymarket"}
        opportunities = []
        seen = set()

        for event in matched_events:
            markets = event.get("markets", [])
            title = event.get("canonical_title", "")
            expiry = event.get("expiry", "")

            for market in markets:
                platform = market.get("platform", "")
                # Skip platforms we can't trade on
                if platform not in tradeable_platforms:
                    continue

                event_id = market.get("event_id", "")
                dedup_key = f"{platform}:{event_id}"
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                yes_price = market.get("yes_price", 0)
                no_price = market.get("no_price", 0)
                # Skip markets with no real prices
                if yes_price < 0.01 and no_price < 0.01:
                    continue
                # Skip markets where either side is essentially zero (no liquidity)
                if yes_price < 0.01 or no_price < 0.01:
                    continue

                # Same filters as _scan_polymarket
                if yes_price > 0.85 or yes_price < 0.15:
                    continue
                if 0.42 < yes_price < 0.58:
                    continue

                volume = market.get("volume", 0)
                conviction = abs(yes_price - 0.5)

                # Days to expiry
                days_to_expiry = 999
                if expiry and expiry != "ongoing":
                    try:
                        exp = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                        days_to_expiry = (exp.date() - date.today()).days
                    except (ValueError, TypeError):
                        try:
                            exp_date = date.fromisoformat(expiry[:10])
                            days_to_expiry = (exp_date - date.today()).days
                        except (ValueError, TypeError):
                            pass

                hours_to_expiry = days_to_expiry * 24
                if hours_to_expiry < MIN_HOURS_TO_EXPIRY:
                    continue

                # Profit potential
                favored_price = min(yes_price, no_price) if no_price > 0 else yes_price
                raw_profit_pct = ((1.0 - favored_price) / favored_price) * 100 if favored_price > 0 else 0
                net_profit_pct = raw_profit_pct - ROUND_TRIP_FEE_PCT

                # Score (same formula as _scan_polymarket)
                score = net_profit_pct
                if 3 <= days_to_expiry <= 14:
                    score *= 2.0
                elif 14 < days_to_expiry <= 30:
                    score *= 1.5
                if volume > 100000:
                    score *= 1.5
                elif volume > 10000:
                    score *= 1.2
                if conviction > 0.3:
                    score *= 1.5
                elif conviction > 0.2:
                    score *= 1.2

                opp = {
                    "title": title or market.get("title", ""),
                    "canonical_title": title or market.get("title", ""),
                    "buy_yes_platform": platform,
                    "buy_yes_price": yes_price,
                    "buy_no_platform": platform,
                    "buy_no_price": no_price if no_price > 0 else 1.0 - yes_price,
                    "buy_yes_market_id": event_id,
                    "buy_no_market_id": event_id,
                    "profit_pct": round(net_profit_pct, 1),
                    "expiry": expiry[:10] if expiry and expiry != "ongoing" else "",
                    "days_to_expiry": days_to_expiry,
                    "volume": volume,
                    "conviction": round(conviction, 3),
                    "_score": score,
                    "_source_platform": platform,
                }
                opportunities.append(opp)

        # Sort by score descending
        opportunities.sort(key=lambda o: o.get("_score", 0), reverse=True)

        # Log platform breakdown
        platform_counts = {}
        for opp in opportunities:
            p = opp.get("_source_platform", "?")
            platform_counts[p] = platform_counts.get(p, 0) + 1
        if platform_counts:
            breakdown = ", ".join(f"{p}={c}" for p, c in sorted(platform_counts.items()))
            logger.info("Auto trader: %d opportunities from all platforms (%s)",
                        len(opportunities), breakdown)

        return opportunities[:15]  # Top 15 across all platforms

    async def _scan_polymarket(self) -> list[dict]:
        """Direct scan of Polymarket Gamma API for high-volume prediction markets.

        Scans ALL categories (crypto, politics, sports, finance, macro) sorted by
        volume. No keyword filter — the scoring system handles selection.
        """
        if not httpx:
            return []

        GAMMA_API = "https://gamma-api.polymarket.com"
        opportunities = []

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Fetch top markets by volume across all categories
                seen_ids = set()
                all_markets = []
                for offset in [0, 100, 200]:
                    try:
                        r = await client.get(f"{GAMMA_API}/markets", params={
                            "closed": "false",
                            "limit": "100",
                            "offset": str(offset),
                            "order": "volume",
                            "ascending": "false",
                        })
                        if r.status_code == 200:
                            batch = r.json()
                            if isinstance(batch, list):
                                all_markets.extend(batch)
                    except Exception:
                        pass
                    await asyncio.sleep(1)

                logger.info("Auto trader: fetched %d markets from Polymarket (all categories)", len(all_markets))

                for market in all_markets:
                    mid = market.get("conditionId") or market.get("id", "")
                    if not mid or mid in seen_ids:
                        continue
                    seen_ids.add(mid)

                    # Parse outcomePrices — it's a JSON string like '["0.475", "0.525"]'
                    raw_prices = market.get("outcomePrices", "[]")
                    if isinstance(raw_prices, str):
                        try:
                            import json as _json
                            parsed = _json.loads(raw_prices)
                        except Exception:
                            parsed = []
                    else:
                        parsed = raw_prices

                    if not parsed or len(parsed) < 1:
                        continue

                    try:
                        yes_price = float(parsed[0]) if parsed[0] else 0.5
                    except (ValueError, TypeError):
                        yes_price = 0.5

                    no_price = 1.0 - yes_price

                    # Skip if too close to resolved (>0.85 or <0.15) — tiny upside not worth risk
                    if yes_price > 0.85 or yes_price < 0.15:
                        continue
                    # Skip near-50/50 markets — no conviction edge
                    if 0.42 < yes_price < 0.58:
                        continue

                    title = market.get("question", market.get("title", ""))
                    end_date = market.get("endDate", market.get("expirationDate", ""))

                    # Check expiry
                    days_to_expiry = 999
                    if end_date:
                        try:
                            exp = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                            days_to_expiry = (exp.date() - date.today()).days
                        except (ValueError, TypeError):
                            pass

                    hours_to_expiry = days_to_expiry * 24
                    if hours_to_expiry < MIN_HOURS_TO_EXPIRY:
                        continue

                    # Score based on conviction (distance from 0.5)
                    conviction = abs(yes_price - 0.5)
                    volume = float(market.get("volumeNum", 0) or market.get("volume", 0) or 0)

                    # Profit potential: how much can be gained AFTER round-trip fees
                    # Buy the favored side, if it resolves to 1.0 we get (1.0 - buy_price) profit
                    # minus round-trip fees on the trade amount
                    favored_price = min(yes_price, no_price)  # buy the cheaper side
                    raw_profit_pct = ((1.0 - favored_price) / favored_price) * 100 if favored_price > 0 else 0
                    # Deduct estimated round-trip fees
                    net_profit_pct = raw_profit_pct - ROUND_TRIP_FEE_PCT

                    opp = {
                        "title": title,
                        "canonical_title": title,
                        "buy_yes_platform": "polymarket",
                        "buy_yes_price": yes_price,
                        "buy_no_platform": "polymarket",
                        "buy_no_price": no_price,
                        "buy_yes_market_id": mid,
                        "buy_no_market_id": mid,
                        "profit_pct": round(net_profit_pct, 1),
                        "raw_profit_pct": round(raw_profit_pct, 1),
                        "estimated_fees_pct": ROUND_TRIP_FEE_PCT,
                        "expiry": end_date[:10] if end_date else "",
                        "days_to_expiry": days_to_expiry,
                        "volume": volume,
                        "conviction": round(conviction, 3),
                    }
                    opportunities.append(opp)

        except Exception as e:
            logger.warning("Auto trader: Polymarket scan failed: %s", e)

        # Sort by risk/reward score
        for opp in opportunities:
            score = opp["profit_pct"]
            dte = opp.get("days_to_expiry", 999)
            vol = opp.get("volume", 0)
            conv = opp.get("conviction", 0)

            # Near-expiry bonus (3-14 days is sweet spot — enough time to move, close to resolution)
            if 3 <= dte <= 14:
                score *= 2.0
            elif 14 < dte <= 30:
                score *= 1.5

            # Volume = liquidity = better execution
            if vol > 100000:
                score *= 1.5
            elif vol > 10000:
                score *= 1.2

            # High conviction (price far from 0.5) = market has formed opinion = more edge
            if conv > 0.3:
                score *= 1.5
            elif conv > 0.2:
                score *= 1.2

            opp["_score"] = score

        opportunities.sort(key=lambda o: o.get("_score", 0), reverse=True)
        logger.info("Auto trader: found %d markets on Polymarket (top score=%.1f)",
                     len(opportunities), opportunities[0]["_score"] if opportunities else 0)
        return opportunities[:10]  # Top 10 across all categories

    def get_stats(self) -> dict:
        open_pkgs = self.pm.list_packages("open")
        return {
            "running": self._running,
            "trades_opened": self._trades_opened,
            "trades_skipped": self._trades_skipped,
            "open_positions": len(open_pkgs),
            "total_exposure": round(sum(p.get("total_cost", 0) for p in open_pkgs), 2),
            "max_exposure": MAX_TOTAL_EXPOSURE,
            "scan_interval_sec": self.interval,
            "trades_today": self._daily_trade_count,
            "max_trades_per_day": MAX_NEW_TRADES_PER_DAY,
        }
