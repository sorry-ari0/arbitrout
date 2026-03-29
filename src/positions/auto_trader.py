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

# Position limits — derived from bankroll at runtime
MAX_CONCURRENT = 20          # Max 20 open packages (raised from 10 — was triggering insider_only_mode too early)
INSIDER_EXTRA_SLOTS = 3      # Extra slots beyond MAX_CONCURRENT for insider-signaled trades
NEWS_EXTRA_SLOTS = 2         # Extra slots beyond MAX_CONCURRENT for news-driven trades
PORTFOLIO_EXPOSURE_CAP = 0.40  # Kelly portfolio rule: never exceed 40% of total bankroll

# Bankroll -> dollar limit ratios
_RATIO_MAX_TRADE = 0.025       # $50 / $2000
_RATIO_MIN_TRADE = 0.005       # $10 / $2000, with $1.00 floor for Polymarket practicality
_RATIO_MAX_EXPOSURE = 0.50     # 50% of bankroll ($10 at $20, $1000 at $2000)
_MIN_TRADE_FLOOR = 1.0         # Polymarket practical minimum
SCAN_INTERVAL = 300          # 5 minutes between self-initiated scans (safety net)
MIN_SPREAD_PCT = 8.0         # Minimum 8% spread (reduced: 0% maker fees both sides)
# Polymarket: 0% maker fee on GTC limit orders for BOTH entry and exit.
# All orders (buy + sell) now use GTC limit at spread edge = 0% round-trip.
# With 8% min spread - 0% fees = 8% net margin minimum.
# Lowered from 12%: with 0% fees we can capture more opportunities.
ROUND_TRIP_FEE_PCT = 0.0     # 0% round-trip fees (maker orders both sides)
MAX_LOSSES_PER_MARKET = 2    # Block market after 2 losses (prevents BTC-top-performer pattern: 6 entries, $24 lost)
MAX_NEW_TRADES_PER_DAY = 3          # Max new positions per calendar day
MARKET_COOLDOWN_SECONDS = 172800    # 48h cooldown per market (was 86400)
MIN_HOURS_TO_EXPIRY = 1.0  # Skip markets expiring within 1 hour (dynamic fees, bot dominance)

# Reserve 40% of max exposure for cross-platform arb (highest conviction, guaranteed profit).
# Directional bets can only consume the remaining 60%. This prevents arb starvation
# that the decision log showed: 30-43% spread arbs repeatedly blocked.
ARB_BUDGET_RESERVE_PCT = 0.40

# Hold to resolution for short-expiry prediction markets (<= this many days).
# Research: trailing stops (-$98) and time exits (-$39) destroyed value.
HOLD_TO_RESOLUTION_MAX_DAYS = 14

# ── Portfolio correlation / concentration limits ──────────────────────────────
# Research: max 20-30% in one sector. Count correlated positions as single exposure.
MAX_CATEGORY_CONCENTRATION = 0.50  # No more than 50% of total exposure in one category
# CATEGORY_KEYWORDS defined after SPORTS_KEYWORDS below

# ── Regime detection (5-loss rule) ────────────────────────────────────────────
# Research: after 5 consecutive losses, cut position sizes 50% until a win
# This prevents drawdown spirals and forces the system to wait for regime change
LOSS_STREAK_THRESHOLD = 5    # Consecutive losses before regime reduction
REGIME_SIZE_REDUCTION = 0.50 # Multiply position sizes by this during bad regime

# ── Kelly sizing defaults for non-prediction trade types ──────────────────────
# Research: Half Kelly = 75% of full Kelly growth with 50% less drawdown
# Quarter Kelly retains 56% of max growth with ~3% chance of halving bankroll
KELLY_EDGE_BY_STRATEGY = {
    "multi_outcome_arb": 0.10,       # 10% edge — near-guaranteed profit
    "portfolio_no": 0.08,            # 8% edge — strong structural advantage
    "weather_forecast": 0.05,        # 5% edge — NWS data advantage
    "political_synthetic": 0.03,     # 3% edge — LLM-derived, uncertain
    "crypto_synthetic": 0.03,        # 3% edge — LLM-derived, uncertain
    "cross_platform_arb": 0.12,      # 12% edge — guaranteed spread
    "synthetic_derivative": 0.04,    # 4% edge — structural but uncertain
}
# ── Signal decay (news urgency) ───────────────────────────────────────────────
# Research: news signals have minutes-to-hours half-life. Fresh = full edge.
# Signal age → score multiplier (linear decay within each bucket)
SIGNAL_DECAY_TIERS = [
    (5 * 60,    1.0),    # 0-5 min: full score
    (30 * 60,   0.7),    # 5-30 min: 70%
    (60 * 60,   0.4),    # 30-60 min: 40%
    (float('inf'), 0.1), # >60 min: 10% (stale signal)
]

KELLY_FRACTION_BY_STRATEGY = {
    "multi_outcome_arb": 0.50,       # Half Kelly — high confidence
    "portfolio_no": 0.50,            # Half Kelly — high confidence
    "weather_forecast": 0.25,        # Quarter Kelly — moderate confidence
    "political_synthetic": 0.20,     # 1/5 Kelly — uncertain
    "crypto_synthetic": 0.20,        # 1/5 Kelly — uncertain
    "cross_platform_arb": 0.50,      # Half Kelly — guaranteed profit
    "synthetic_derivative": 0.25,    # Quarter Kelly — structural edge
}

# Market category keywords — shared with exit_engine.py for consistency
# Journal analysis: sports -$91.99/10 trades (20% WR), commodities -$45.76/3 trades (0% WR)
SPORTS_KEYWORDS = [
    "score", "ncaa", "nba", "nfl", "nhl", "mlb", "epl", "la liga",
    "bundesliga", "serie a", "ligue 1", "uefa", "champions league",
    "premier league", "euroleague", "ufc", "mma", "fight night",
    "boxing", "formula 1", "f1", "grand prix", "nascar",
    "atp", "wta", "wimbledon", "tournament", "playoff",
    "super bowl", "world cup", "world series", "vs.", "vs ",
    "match", "game", "winner",
]
COMMODITIES_KEYWORDS = [
    "crude oil", "wti", "brent", "natural gas", "gold price",
    "silver price", "copper",
]

# Portfolio correlation category detection (uses SPORTS_KEYWORDS defined above)
CATEGORY_KEYWORDS = {
    "crypto": ["btc", "bitcoin", "eth", "ethereum", "crypto", "solana", "sol", "xrp",
               "cardano", "dogecoin", "doge", "bnb", "ripple", "avalanche", "polygon"],
    "politics": ["president", "election", "congress", "senate", "governor", "democrat",
                 "republican", "trump", "biden", "nomination", "primary", "impeach",
                 "supreme court", "legislation", "bill pass"],
    "sports": SPORTS_KEYWORDS,
    "weather": ["temperature", "weather", "rainfall", "hurricane", "tornado", "snowfall",
                "heat wave", "cold", "precipitation", "forecast"],
    "finance": ["fed", "interest rate", "gdp", "inflation", "unemployment", "cpi",
                "fomc", "treasury", "yield", "recession", "tariff", "trade war"],
}


class AutoTrader:
    """Autonomous paper trader that creates packages from scanner opportunities."""

    def __init__(self, position_manager, scanner=None, insider_tracker=None,
                 interval: float = SCAN_INTERVAL, decision_logger=None,
                 probability_model=None, initial_bankroll: float = 2000.0,
                 paper_mode: bool = True):
        self.pm = position_manager
        self.scanner = scanner
        self.insider_tracker = insider_tracker
        self.interval = interval
        self.dlog = decision_logger
        self.probability_model = probability_model
        self._initial_bankroll = initial_bankroll
        self._paper_mode = paper_mode
        self._task = None
        self._running = False
        self._trades_opened = 0
        self._trades_skipped = 0
        self._skip_reasons: dict[str, int] = {}  # reason → count
        self._last_trade_time = 0.0
        self._daily_trade_count = 0
        self._daily_trade_date = ""
        self._session_start_time = time.time()  # Tracks server start for daily limit
        self._scan_event = asyncio.Event()  # Fired by arb scanner after each scan
        # News scanner integration — thread-safe queue
        self._news_lock = asyncio.Lock()
        self._news_opportunities: list[dict] = []
        self._political_analyzer = None
        self._weather_scanner = None
        self.kyle_estimator = None
        self._loss_streak = 0         # Current consecutive losses
        self._regime_penalty = 1.0    # 1.0 = normal, 0.5 = bad regime
        self._refresh_limits()

    def _get_current_bankroll(self) -> float:
        """Current bankroll = initial + cumulative P&L from journal."""
        pnl = 0.0
        if self.pm.trade_journal:
            pnl = self.pm.trade_journal.get_cumulative_pnl()
        return self._initial_bankroll + pnl

    def _refresh_limits(self):
        """Recompute dollar-denominated limits from current bankroll."""
        bankroll = self._get_current_bankroll()
        self._max_trade_size = round(bankroll * _RATIO_MAX_TRADE, 2)
        self._min_trade_size = round(max(_MIN_TRADE_FLOOR, bankroll * _RATIO_MIN_TRADE), 2)
        self._max_total_exposure = round(bankroll * _RATIO_MAX_EXPOSURE, 2)
        self._total_bankroll = bankroll

    def set_political_analyzer(self, analyzer):
        """Set the political analyzer reference for opportunity consumption."""
        self._political_analyzer = analyzer

    def set_weather_scanner(self, scanner):
        """Set the weather scanner reference for opportunity consumption."""
        self._weather_scanner = scanner

    def set_kyle_estimator(self, estimator):
        """Set the Kyle's lambda estimator for adverse selection scoring."""
        self.kyle_estimator = estimator

    @staticmethod
    def _detect_category(title: str) -> str:
        """Detect market category from title for concentration tracking."""
        title_lower = title.lower()
        for category, keywords in CATEGORY_KEYWORDS.items():
            if any(kw in title_lower for kw in keywords):
                return category
        return "other"

    def _get_category_exposure(self, open_pkgs: list[dict]) -> dict[str, float]:
        """Calculate current exposure per category."""
        exposure: dict[str, float] = {}
        for pkg in open_pkgs:
            name = pkg.get("name", "")
            cat = self._detect_category(name)
            cost = pkg.get("total_cost", 0)
            exposure[cat] = exposure.get(cat, 0) + cost
        return exposure

    def _check_concentration(self, title: str, trade_size: float,
                             total_exposure: float, category_exposure: dict[str, float],
                             max_concentration: float = MAX_CATEGORY_CONCENTRATION) -> bool:
        """Check if adding this trade would exceed category concentration limit.

        Returns True if trade is allowed, False if it would over-concentrate.
        max_concentration: override limit (default 50%, use 75% for news/insider).
        """
        if total_exposure + trade_size <= 0:
            return True
        # Allow trades when portfolio is small (< 3 positions worth)
        # — can't diversify a near-empty portfolio
        if total_exposure < self._min_trade_size * 3:
            return True
        cat = self._detect_category(title)
        new_cat_exposure = category_exposure.get(cat, 0) + trade_size
        new_total = total_exposure + trade_size
        concentration = new_cat_exposure / new_total
        return concentration <= max_concentration

    @staticmethod
    def _signal_decay(signal_created_at: float) -> float:
        """Calculate decay multiplier based on signal age.

        Research: news signals have minutes-to-hours half-life.
        Fresh signals get full score, stale signals get 10%.
        """
        if not signal_created_at:
            return 1.0
        age_seconds = time.time() - signal_created_at
        if age_seconds < 0:
            return 1.0
        for max_age, multiplier in SIGNAL_DECAY_TIERS:
            if age_seconds < max_age:
                return multiplier
        return 0.1

    def _update_regime(self):
        """Check trade journal for consecutive loss streak and adjust regime penalty.

        Research: 5 consecutive losses → cut position sizes 50% until a win.
        Prevents drawdown spirals during adverse market regimes.
        """
        if not self.pm.trade_journal:
            return
        entries = self.pm.trade_journal.get_recent(limit=LOSS_STREAK_THRESHOLD + 5)
        if not entries:
            self._loss_streak = 0
            self._regime_penalty = 1.0
            return

        # Count consecutive losses from most recent trade backward
        streak = 0
        for entry in entries:  # Already sorted most recent first
            if entry.get("outcome") == "loss":
                streak += 1
            else:
                break

        self._loss_streak = streak
        if streak >= LOSS_STREAK_THRESHOLD:
            self._regime_penalty = REGIME_SIZE_REDUCTION
            logger.warning("REGIME: %d consecutive losses — reducing position sizes by %.0f%%",
                           streak, (1 - REGIME_SIZE_REDUCTION) * 100)
        else:
            self._regime_penalty = 1.0

    def _kelly_size(self, strategy: str, remaining_budget: float,
                    implied_prob: float = 0.0, spread_pct: float = 0.0,
                    bypass_regime: bool = False) -> float:
        """Calculate Kelly-optimal position size for any strategy type.

        Research: Half Kelly = 75% growth with 50% less drawdown.
        Returns sized trade amount capped at self._max_trade_size, floored at self._min_trade_size.
        """
        edge = KELLY_EDGE_BY_STRATEGY.get(strategy, 0.02)
        frac = KELLY_FRACTION_BY_STRATEGY.get(strategy, 0.25)

        # For strategies with known spread, use actual spread as edge estimate
        if spread_pct > 0:
            edge = max(edge, spread_pct / 100.0)

        # Kelly: f* = edge / odds, simplified for binary: f* = 2*p - 1 where p = 0.5 + edge/2
        # More precisely: f* = (b*p - q) / b where b = net odds
        if implied_prob > 0:
            p_true = min(0.95, implied_prob + edge)
            b = (1.0 - implied_prob) / implied_prob if implied_prob > 0 else 1.0
            kelly_full = (b * p_true - (1.0 - p_true)) / b if b > 0 else 0
        else:
            # No implied probability — use edge directly
            kelly_full = edge

        kelly_sized = max(0.0, kelly_full * frac)
        sized = round(min(self._max_trade_size, remaining_budget * kelly_sized), 2)
        # Bad regime (5+ consecutive losses): skip entirely instead of limping
        # in with min-size bets that waste concurrent slots and can't produce
        # meaningful returns. Resume trading when streak breaks.
        if self._regime_penalty < 1.0 and not bypass_regime:
            return 0
        return max(self._min_trade_size, min(sized, self._max_trade_size))

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
        mode = "PAPER" if self._paper_mode else "LIVE"
        logger.info("Auto trader started (mode=%s, interval=%.0fs, max_exposure=$%.0f)", mode, self.interval, self._max_total_exposure)

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
                    condition_id = (asset_id.split(":")[0] if ":" in asset_id else asset_id).lower()
                    if condition_id:
                        ids.add(condition_id)
        return ids

    def _check_daily_limit(self) -> bool:
        """Returns True if we can still open trades today.

        Only counts trades opened during THIS session (after server start).
        Pre-existing positions loaded on restart do NOT count — they were
        already counted in the session that created them.
        """
        today = date.today().isoformat()
        if self._daily_trade_date != today:
            # Reset in-memory counter and recount from persisted packages
            self._daily_trade_count = 0
            self._daily_trade_date = today
            for p in self.pm.list_packages():
                # Only count trades created AFTER this server started
                if (p.get("created_at", 0) >= self._session_start_time
                        and p.get("name", "").startswith("Auto:")):
                    self._daily_trade_count += 1
        return self._daily_trade_count < MAX_NEW_TRADES_PER_DAY

    async def _scan_and_trade(self):
        """One scan cycle: find opportunities, filter, create packages."""
        # Update regime state from trade journal (5-loss rule)
        self._update_regime()
        # Recompute dollar limits from current bankroll (initial + P&L)
        self._refresh_limits()

        open_pkgs = self.pm.list_packages("open")
        insider_only_mode = False
        news_only_mode = False
        _hard_max = MAX_CONCURRENT + max(INSIDER_EXTRA_SLOTS, NEWS_EXTRA_SLOTS)
        if len(open_pkgs) >= MAX_CONCURRENT:
            if len(open_pkgs) >= _hard_max:
                logger.info("Auto trader: at hard max (%d/%d), skipping",
                            len(open_pkgs), _hard_max)
                if self.dlog:
                    self.dlog.log_scan_skip("max_concurrent", open_positions=len(open_pkgs))
                return
            # In the extra-slots zone: only allow insider-signaled or news-driven trades
            insider_only_mode = True
            news_only_mode = True
            logger.info("Auto trader: at max concurrent (%d/%d), insider+news only mode (%d extra slots)",
                         len(open_pkgs), MAX_CONCURRENT, _hard_max - len(open_pkgs))

        total_exposure = sum(p.get("total_cost", 0) for p in open_pkgs)
        if total_exposure >= self._max_total_exposure:
            logger.info("Auto trader: at max exposure ($%.2f), skipping", total_exposure)
            if self.dlog:
                self.dlog.log_scan_skip("max_exposure", exposure=round(total_exposure, 2))
            return

        # Kelly portfolio rule: total exposure across ALL positions must not exceed
        # 40% of bankroll. This prevents over-concentration even when individual
        # Kelly fractions are correct. (Research: reduces 80% drawdown probability
        # from 1-in-5 to 1-in-213 at 30% Kelly, we use 40% as generous cap.)
        kelly_cap = self._total_bankroll * PORTFOLIO_EXPOSURE_CAP
        if total_exposure >= kelly_cap:
            logger.info("Auto trader: at Kelly portfolio cap ($%.2f / $%.2f), skipping",
                        total_exposure, kelly_cap)
            if self.dlog:
                self.dlog.log_scan_skip("kelly_portfolio_cap", exposure=round(total_exposure, 2))
            return

        daily_limit_hit = not self._check_daily_limit()
        if daily_limit_hit:
            logger.info("Auto trader: daily limit (%d/%d), scanning for guaranteed-profit only",
                        self._daily_trade_count, MAX_NEW_TRADES_PER_DAY)
            if self.dlog:
                self.dlog.log_scan_skip("daily_limit", trades_today=self._daily_trade_count)

        remaining_budget = min(self._max_total_exposure - total_exposure, kelly_cap - total_exposure)
        effective_max = _hard_max if (insider_only_mode or news_only_mode) else MAX_CONCURRENT
        remaining_slots = effective_max - len(open_pkgs)

        # Split budget: reserve ARB_BUDGET_RESERVE_PCT for cross-platform arbs.
        # Directional bets can only use the unreserved portion.
        arb_reserve = self._max_total_exposure * ARB_BUDGET_RESERVE_PCT
        # How much of the reserve is already consumed by existing arb packages?
        arb_exposure = sum(p.get("total_cost", 0) for p in open_pkgs
                          if p.get("strategy_type") in ("cross_platform_arb", "multi_outcome_arb"))
        arb_remaining_reserve = max(0, arb_reserve - arb_exposure)
        # Directional budget = total remaining MINUS the unfilled arb reserve
        directional_budget = max(0, remaining_budget - arb_remaining_reserve)
        # Arb budget = full remaining (arbs can use both their reserve AND any leftover)
        arb_budget = remaining_budget

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
                        opp["_score"] = opp.get("net_profit_pct", opp.get("profit_pct", 0)) * 3.0  # Arb premium (use net after fees)
                        opportunities.append(opp)

                # 2. Single-platform directional bets from ALL platform events
                platform_opps = self._events_to_opportunities(all_events)
                opportunities.extend(platform_opps)

            except Exception as e:
                logger.warning("Auto trader: scanner failed: %s", e)

        if not opportunities:
            # Fallback: direct Polymarket scan (only if scanner failed entirely)
            opportunities = await self._scan_polymarket()

        # Merge queued news opportunities with score boost + signal decay
        # News-driven entries can bypass dedup to add to existing positions
        news_opps = await self._drain_news_opportunities()
        for news_opp in news_opps:
            base_score = news_opp.get("_score", 10.0) * 2.0  # News edge boost
            # Apply signal decay: fresh signals get full score, stale ones penalized
            decay = self._signal_decay(news_opp.get("signal_created_at", 0))
            news_opp["_score"] = base_score * decay
            news_opp["_signal_decay"] = decay
            news_opp["_news_driven"] = True
            opportunities.append(news_opp)
        if news_opps:
            decayed = sum(1 for o in news_opps if o.get("_signal_decay", 1.0) < 1.0)
            logger.info("Auto trader: merged %d news opportunities (%d decayed)", len(news_opps), decayed)

        # Merge multi-outcome, portfolio NO, and weather scans in parallel
        parallel_tasks = []
        task_labels = []
        if self.scanner:
            parallel_tasks.append(self.scanner.scan_multi_outcome())
            task_labels.append("multi_outcome")
            parallel_tasks.append(self.scanner.scan_portfolio_no())
            task_labels.append("portfolio_no")
        if self._weather_scanner:
            parallel_tasks.append(self._weather_scanner.scan())
            task_labels.append("weather")

        if parallel_tasks:
            results = await asyncio.gather(*parallel_tasks, return_exceptions=True)
            for label, result in zip(task_labels, results):
                if isinstance(result, Exception):
                    logger.warning("Auto trader: %s scan failed: %s", label, result)
                    continue
                for opp in result:
                    if label == "multi_outcome":
                        opp["_score"] = opp.get("profit_pct", 0) * 5.0
                    elif label == "portfolio_no":
                        opp["_score"] = opp.get("profit_pct", 0) * 4.0
                    elif label == "weather":
                        opp["_score"] = opp.get("edge", 0) * 100 * 3.0
                    opportunities.append(opp)
                if result:
                    logger.info("Auto trader: merged %d %s opportunities", len(result), label)

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

        # Portfolio correlation tracking — compute category exposure
        category_exposure = self._get_category_exposure(open_pkgs)

        trades_this_cycle = 0
        insider_mode_passed_filter_count = 0
        insider_mode_skipped_filter_count = 0
        opportunities.sort(key=lambda o: o.get("_score", 0), reverse=True)
        for opp in opportunities:
            opp_title = (opp.get("title") or opp.get("canonical_title") or "?")[:100]
            opp_volume = opp.get("volume", 0)  # Market volume — logged on all skips for analysis
            opp_strategy = opp.get("opportunity_type", "")
            if trades_this_cycle >= remaining_slots:
                break
            if directional_budget < self._min_trade_size and arb_budget < self._min_trade_size:
                break

            # Filter: skip zero-price markets (no liquidity, phantom opportunities)
            buy_yes_price = opp.get("buy_yes_price", 0)
            buy_no_price = opp.get("buy_no_price", 0)
            if buy_yes_price < 0.01 and buy_no_price < 0.01:
                self._record_skip("zero_price")
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "zero_price",
                                                   volume=opp_volume, strategy=opp_strategy)
                continue

            # Filter: require minimum spread (use net profit after fees when available)
            spread_pct = opp.get("net_profit_pct") or opp.get("profit_pct", 0)
            if spread_pct < MIN_SPREAD_PCT:
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "low_spread", spread_pct=spread_pct,
                                                   volume=opp_volume, strategy=opp_strategy)
                continue

            # Skip markets we already have positions on (check BOTH sides, case-insensitive)
            # EXCEPTION: allow re-entry when news or insider signals provide new information
            has_signal = bool(opp.get("insider_signal") or opp.get("_news_driven") or opp.get("_insider_driven"))
            yes_mid = opp.get("buy_yes_market_id", "").lower()
            no_mid = opp.get("buy_no_market_id", "").lower()
            if not has_signal and ((yes_mid and yes_mid in open_market_ids) or (no_mid and no_mid in open_market_ids)):
                self._record_skip("already_open")
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "already_open",
                                                   volume=opp_volume, strategy=opp_strategy)
                continue

            # Prioritize crypto-related and near-expiry
            title = (opp.get("title") or opp.get("canonical_title") or "").lower()
            is_crypto = any(kw in title for kw in ["btc", "bitcoin", "eth", "ethereum", "crypto", "solana", "sol", "xrp"])

            # Market category filter: penalize historically unprofitable categories
            # Trade journal (39 trades): sports -$91.99 (10 trades, 20% WR),
            # commodities -$45.76 (3 trades, 0% WR). Exact-score bets are worst.
            is_sports_exact_score = "exact score" in title
            is_ncaa = "ncaa" in title
            is_sports = any(kw in title for kw in SPORTS_KEYWORDS)
            is_commodities = any(kw in title for kw in COMMODITIES_KEYWORDS)

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

            # Guaranteed-profit strategies (multi_outcome_arb, portfolio_no) resolve
            # at exactly $1.00 regardless of timing — skip duration filters entirely.
            opp_type = opp.get("opportunity_type", "")
            guaranteed_profit = opp_type in ("multi_outcome_arb", "portfolio_no")

            # Skip short-duration markets (15-min, 1-hour crypto)
            # Research: dynamic fees up to 3.15%, 73% of arb captured by sub-100ms bots
            # Bypass: guaranteed-profit arbs have only upside at any duration.
            if hours_to_expiry < MIN_HOURS_TO_EXPIRY and not guaranteed_profit:
                self._record_skip("short_duration")
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "short_duration",
                                                   hours=round(hours_to_expiry, 1),
                                                   volume=opp_volume, strategy=opp_strategy)
                continue

            # Skip markets expiring within 2 days
            # Bypass: guaranteed-profit arbs profit at resolution, short expiry is fine.
            if days_to_expiry <= 2 and not guaranteed_profit:
                self._record_skip("too_near_expiry")
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "too_near_expiry", days=days_to_expiry,
                                                   volume=opp_volume, strategy=opp_strategy)
                continue

            # Score: crypto near-expiry > crypto > near-expiry > other
            score = spread_pct
            crypto_mult = 2.0 if is_crypto else 1.0
            score *= crypto_mult
            expiry_mult = 1.5 if is_near_expiry else 1.0
            score *= expiry_mult

            # Market category penalties (journal-driven)
            if is_sports_exact_score:
                # Exact-score bets: -$24 from 3 trades, 0% win rate. Skip entirely.
                self._record_skip("exact_score_market")
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "exact_score_market",
                                                   volume=opp_volume, strategy=opp_strategy)
                continue
            if is_ncaa:
                # NCAA: -$68 from 5 trades, 0% win rate (all trailing stop losses at -13.5%)
                score *= 0.1  # Heavy penalty, effectively blocks unless massive spread
            elif is_sports:
                # Other sports: -$92 total, 20% win rate. Discount heavily.
                score *= 0.3
            # Hard skip — 0% WR across 3 trades, -$46
            if is_commodities:
                self._record_skip("commodities_market")
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "commodities_market",
                                                   volume=opp_volume, strategy=opp_strategy)
                continue

            # Favorite-longshot bias (research-validated):
            # Research: longshots lose ~40%, favorites lose ~5%
            # Kalshi: contracts <$0.10 lose >60% of buyer's money
            # Academic evidence: markets systematically overprice longshots,
            # underprice favorites. This is a documented, persistent edge.
            #
            # For synthetics: use structural win probability, not individual leg prices.
            # A synthetic with 75% win_prob and $0.20 leg price is NOT a longshot —
            # the $0.20 is a component price, not a directional bet.
            _is_synth_opp = opp.get("is_synthetic", False)
            if _is_synth_opp:
                win_prob = 1.0 - opp.get("synthetic_info", {}).get("loss_probability", 0.5)
                if win_prob >= 0.80:
                    favorite_mult = 3.0
                elif win_prob >= 0.70:
                    favorite_mult = 2.2
                elif win_prob >= 0.60:
                    favorite_mult = 1.4
                elif win_prob <= 0.30:
                    favorite_mult = 0.5
                else:
                    favorite_mult = 1.0
            else:
                favored = max(buy_yes_price, buy_no_price) if buy_no_price > 0 else buy_yes_price
                if favored >= 0.80:
                    favorite_mult = 3.0  # Strong favorite — strongest documented edge
                elif favored >= 0.70:
                    favorite_mult = 2.2  # Moderate favorite — solid edge
                elif favored >= 0.60:
                    favorite_mult = 1.4  # Mild favorite — still has bias edge
                elif favored <= 0.15:
                    favorite_mult = 0.1  # Extreme longshot — near-zero expected value
                elif favored <= 0.20:
                    favorite_mult = 0.2  # Severe longshot penalty
                elif favored <= 0.30:
                    favorite_mult = 0.5  # Longshot penalty
                else:
                    favorite_mult = 1.0  # Neutral zone (0.30 < favored < 0.60)
            score *= favorite_mult

            # Insider signal boost: conviction traders get massive boost, market makers ignored
            insider_signal = None
            insider_mult = 1.0
            market_id = opp.get("buy_yes_market_id", "")
            if not market_id:
                matched = opp.get("matched_event", {})
                for m in matched.get("markets", []):
                    if m.get("platform") == "polymarket":
                        market_id = m.get("market_id", m.get("id", ""))
                        break
            if not market_id and opp.get("buy_yes_platform") != "polymarket":
                market_id = opp.get("buy_no_market_id", "")
            if self.insider_tracker and market_id:
                # Pass market volume for position-relative sizing signal
                opp_volume = opp.get("volume", 0)
                insider_signal = self.insider_tracker.get_insider_signal(market_id, market_volume=opp_volume)
                if insider_signal and insider_signal.get("has_signal"):
                    strength = insider_signal.get("signal_strength", 0)
                    conviction_count = insider_signal.get("conviction_count", 0)
                    if conviction_count > 0:
                        # Conviction traders (Theo4, Fredi9999, etc.) = strong directional signal
                        insider_mult = (1.0 + strength * 3.0)  # Up to 4x base boost
                        score *= insider_mult
                        if conviction_count >= 2:
                            score *= 1.5  # Multiple conviction traders agree = very high signal
                            insider_mult *= 1.5
                    else:
                        # Unknown wallets only — weaker signal
                        insider_mult = (1.0 + strength * 1.5)
                        score *= insider_mult
                    opp["insider_signal"] = insider_signal
                    opp["_insider_driven"] = conviction_count > 0

            # Cross-platform whale convergence: if both Polymarket insiders AND
            # Kalshi anonymous whales are active on the same event, boost/suppress
            cross_platform_mult = 1.0
            if self.insider_tracker and hasattr(self.insider_tracker, 'get_cross_platform_signal'):
                poly_cid = ""
                kalshi_ticker = ""
                kalshi_vol = 0
                yes_plat = opp.get("buy_yes_platform", "")
                no_plat = opp.get("buy_no_platform", "")
                yes_mid = opp.get("buy_yes_market_id", "")
                no_mid = opp.get("buy_no_market_id", "")
                if yes_plat == "polymarket":
                    poly_cid = yes_mid
                elif no_plat == "polymarket":
                    poly_cid = no_mid
                if yes_plat == "kalshi":
                    kalshi_ticker = yes_mid
                    kalshi_vol = opp.get("volume", 0)
                elif no_plat == "kalshi":
                    kalshi_ticker = no_mid
                    kalshi_vol = opp.get("volume", 0)
                if poly_cid and kalshi_ticker:
                    xplat = self.insider_tracker.get_cross_platform_signal(
                        poly_cid, kalshi_ticker, kalshi_volume_24h=kalshi_vol)
                    if xplat.get("convergence") == "aligned" and xplat.get("combined_strength", 0) > 0.2:
                        cross_platform_mult = 1.5
                        score *= cross_platform_mult
                        opp["cross_platform_signal"] = xplat
                    elif xplat.get("convergence") == "conflicting":
                        cross_platform_mult = 0.3  # Heavy penalty for conflicting signals
                        score *= cross_platform_mult
                        opp["cross_platform_signal"] = xplat

            # Cross-platform disagreement boost: if platforms disagree >10%,
            # there may be an informational edge worth capturing
            if self.probability_model:
                consensus = self.probability_model.get_consensus(opp_title)
                if consensus and consensus.get("max_deviation", 0) > 0.10:
                    score *= 1.3

            # Kyle's lambda: adverse selection / informed flow signal
            kyle_mult = 1.0
            if self.kyle_estimator and market_id:
                poly_platform = opp.get("buy_yes_platform", "")
                if poly_platform == "polymarket":
                    kyle_direction = "YES"
                    kyle_market_id = market_id
                elif opp.get("buy_no_platform", "") == "polymarket":
                    kyle_direction = "NO"
                    kyle_market_id = opp.get("buy_no_market_id", market_id)
                else:
                    kyle_direction = "YES"  # fallback
                    kyle_market_id = market_id
                kyle_signal = self.kyle_estimator.get_lambda_signal(kyle_market_id, kyle_direction)
                if kyle_signal:
                    kyle_mult = kyle_signal["multiplier"]
                    # Cap combined signal multiplier — insider and kyle can measure
                    # the same whale trade through two lenses. Use max, not product.
                    if insider_mult > 1.0 and kyle_mult > 1.0:
                        combined = max(insider_mult, kyle_mult)
                        # Undo insider_mult already applied, apply combined cap
                        score = score / insider_mult * combined
                        kyle_mult = 1.0  # already folded into combined
                    else:
                        score *= kyle_mult
                    opp["kyle_signal"] = kyle_signal

            # Log score components for post-hoc analysis (favorite bias audit)
            score_metadata = {
                "raw_spread": spread_pct,
                "crypto_mult": crypto_mult,
                "expiry_mult": expiry_mult,
                "favorite_mult": favorite_mult,
                "insider_mult": round(insider_mult, 4),
                "kyle_mult": round(kyle_mult, 4),
                "cross_platform_mult": round(cross_platform_mult, 4),
                "entry_price": favored,
                "side": "YES" if buy_yes_price <= buy_no_price else "NO",
            }

            # In extra-slots mode: only allow insider-signaled, news-driven,
            # or synthetic trades (synthetics have structural edge, not directional)
            is_news = bool(opp.get("_news_driven"))
            _is_synth = opp.get("is_synthetic", False)
            if (insider_only_mode or news_only_mode) and insider_mult <= 1.0 and cross_platform_mult <= 1.0 and not is_news and not _is_synth:
                self._record_skip("insider_news_only_mode")
                insider_mode_skipped_filter_count += 1
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "insider_news_only_mode",
                                                   insider_mult=insider_mult,
                                                   volume=opp_volume, strategy=opp_strategy)
                continue
            if insider_only_mode or news_only_mode:
                insider_mode_passed_filter_count += 1

            # Skip low-score opportunities
            if score < MIN_SPREAD_PCT:
                self._record_skip("low_score")
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "low_score", score=round(score, 1),
                                                   spread_pct=spread_pct, days_to_expiry=days_to_expiry,
                                                   volume=opp_volume, strategy=opp_strategy)
                continue

            # Portfolio concentration check: skip if one category exceeds limit.
            # Bypass for guaranteed-profit strategies (multi_outcome_arb, portfolio_no).

            # Daily limit: skip non-guaranteed-profit trades when at cap
            if daily_limit_hit and not guaranteed_profit:
                self._record_skip("daily_limit")
                continue

            # News/insider signal-driven trades get a higher concentration limit (75%)
            # since the signal provides additional conviction beyond category diversification.
            is_signal_driven = bool(opp.get("_news_driven") or opp.get("insider_signal"))
            _max_conc = 0.75 if is_signal_driven else MAX_CATEGORY_CONCENTRATION
            if not guaranteed_profit and not self._check_concentration(
                    opp_title, self._min_trade_size,
                    total_exposure, category_exposure, max_concentration=_max_conc):
                self._record_skip("concentration_limit")
                cat = self._detect_category(opp_title)
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "concentration_limit",
                                                   category=cat,
                                                   volume=opp_volume, strategy=opp_strategy)
                continue

            # Size the trade — Kelly for arb/synthetic, pure_prediction uses its own Kelly below
            trade_size = min(self._max_trade_size, directional_budget / 2, directional_budget)
            trade_size = max(self._min_trade_size, trade_size)

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
                self._record_skip("no_market_id")
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "no_market_id",
                                                   volume=opp_volume, strategy=opp_strategy)
                continue

            # Skip legs at price ceiling (>= 0.95) — no upside
            if buy_yes_price >= 0.95:
                yes_market_id = ""  # don't create YES leg
            if buy_no_price >= 0.95:
                no_market_id = ""  # don't create NO leg
            if not yes_market_id and not no_market_id:
                self._record_skip("price_ceiling_both_legs")
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
                self._record_skip("cooldown_after_exit")
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "cooldown_after_exit",
                                                   volume=opp_volume, strategy=opp_strategy)
                continue

            # Title-based duplicate check: don't re-enter the same event by title
            norm_title = trade_title.lower().strip()[:50]
            if norm_title in recently_closed_titles:
                self._record_skip("cooldown_title_match")
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "cooldown_title_match",
                                                   volume=opp_volume, strategy=opp_strategy)
                continue

            # Block markets with too many historical losses
            if market_loss_counts.get(norm_title, 0) >= MAX_LOSSES_PER_MARKET:
                self._record_skip("max_losses_reached")
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, f"max_losses_reached ({market_loss_counts[norm_title]} losses)",
                                                   volume=opp_volume, strategy=opp_strategy)
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
                        self._record_skip("insufficient_price_change")
                        if self.dlog:
                            self.dlog.log_opportunity_skip(
                                opp_title,
                                f"insufficient_price_change ({price_change:.1%} < 10% since last loss exit)",
                                volume=opp_volume, strategy=opp_strategy)
                        continue

            # Also check open positions by title — don't open duplicates of existing positions
            # (news/insider signals can override to allow adding to a position)
            if not has_signal:
                open_titles = set()
                for p in self.pm.list_packages("open"):
                    ptitle = (p.get("name", "").replace("Auto: ", "").replace("News: ", "").lower().strip())[:50]
                    if ptitle:
                        open_titles.add(ptitle)
                if norm_title in open_titles:
                    self._record_skip("duplicate_open_position")
                    if self.dlog:
                        self.dlog.log_opportunity_skip(opp_title, "duplicate_open_position",
                                                       volume=opp_volume, strategy=opp_strategy)
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

                # Kelly-sized trade (Half Kelly for near-guaranteed arb)
                trade_size = self._kelly_size("multi_outcome_arb", arb_budget,
                                              spread_pct=spread_pct, bypass_regime=True)
                if trade_size <= 0:
                    continue

                for outcome in outcomes:
                    leg_price = outcome.get("yes_price", 0)
                    if leg_price <= 0:
                        continue
                    leg_cost = round(trade_size * (leg_price / total_price), 2)
                    leg_cost = max(self._min_trade_size, leg_cost)

                    pkg["legs"].append(create_leg(
                        platform="polymarket",
                        leg_type="prediction_yes",
                        asset_id=f"{outcome['condition_id']}:YES",
                        asset_label=f"YES: {outcome.get('title', '?')[:40]}",
                        entry_price=leg_price,
                        cost=leg_cost,
                        expiry=opp.get("expiry", "2026-12-31")[:10],
                    ))

                # Multi-outcome arb: guaranteed profit — hold to resolution
                # NO stop loss — journal shows stop-losses destroy EV on prediction markets.
                pkg["_hold_to_resolution"] = True

                if not pkg["legs"]:
                    self._record_skip("multi_outcome_no_legs")
                    continue

                # Track actual cost (may exceed trade_size due to min_trade_size floors)
                actual_cost = sum(l["cost"] for l in pkg["legs"])
                if actual_cost > arb_budget:
                    self._record_skip("multi_outcome_over_budget")
                    continue

                pkg["_use_limit_orders"] = True
                pkg["_category"] = self._detect_category(opp_title)
                pkg_name = pkg.get("name", opp_title)
                try:
                    result = await self.pm.execute_package(pkg)
                    if result.get("success"):
                        trades_this_cycle += 1
                        self._trades_opened += 1
                        self._daily_trade_count += 1
                        arb_budget -= actual_cost
                        total_exposure += actual_cost
                        _cat = self._detect_category(opp_title)
                        category_exposure[_cat] = category_exposure.get(_cat, 0) + actual_cost
                        # Refresh open IDs for this cycle
                        for leg in pkg.get("legs", []):
                            cid = leg.get("asset_id", "").split(":")[0]
                            if cid:
                                open_market_ids.add(cid)
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

            # Portfolio NO: buy NO on all non-favorites in multi-outcome events
            if opp.get("opportunity_type") == "portfolio_no":
                try:
                    pkg = create_package(f"Auto: {trade_title[:60]}", "portfolio_no")
                except ValueError:
                    continue

                no_targets = opp.get("no_targets", [])
                if not no_targets:
                    continue

                # Kelly-sized trade (Half Kelly for near-guaranteed profit)
                trade_size = self._kelly_size("portfolio_no", arb_budget,
                                              spread_pct=spread_pct, bypass_regime=True)
                if trade_size <= 0:
                    continue

                # Allocate proportionally to each NO price
                total_no_cost = sum(o.get("no_price", 0) for o in no_targets)
                if total_no_cost <= 0:
                    continue

                for target in no_targets:
                    no_price = target.get("no_price", 0)
                    if no_price <= 0:
                        continue
                    leg_cost = round(trade_size * (no_price / total_no_cost), 2)
                    leg_cost = max(self._min_trade_size, leg_cost)

                    pkg["legs"].append(create_leg(
                        platform="polymarket",
                        leg_type="prediction_no",
                        asset_id=f"{target['condition_id']}:NO",
                        asset_label=f"NO: {target.get('title', '?')[:40]}",
                        entry_price=no_price,
                        cost=leg_cost,
                        expiry=opp.get("expiry", "2026-12-31")[:10],
                    ))

                # Near-guaranteed profit — hold to resolution
                # NO stop loss — journal shows stop-losses destroy EV.
                pkg["exit_rules"].append(create_exit_rule("target_profit", {"target_pct": 15}))

                if not pkg["legs"]:
                    self._record_skip("portfolio_no_no_legs")
                    continue

                # Track actual cost (may exceed trade_size due to min_trade_size floors)
                actual_cost = sum(l["cost"] for l in pkg["legs"])
                if actual_cost > arb_budget:
                    self._record_skip("portfolio_no_over_budget")
                    continue

                pkg["_use_limit_orders"] = True
                pkg["_use_brackets"] = True  # GTC target sell at 0% maker fee
                pkg["_hold_to_resolution"] = True  # Near-guaranteed profit — hold to resolution
                pkg_name = pkg.get("name", opp_title)
                try:
                    result = await self.pm.execute_package(pkg)
                    if result.get("success"):
                        trades_this_cycle += 1
                        self._trades_opened += 1
                        self._daily_trade_count += 1
                        arb_budget -= actual_cost
                        total_exposure += actual_cost
                        _cat = self._detect_category(opp_title)
                        category_exposure[_cat] = category_exposure.get(_cat, 0) + actual_cost
                        for leg in pkg.get("legs", []):
                            cid = leg.get("asset_id", "").split(":")[0]
                            if cid:
                                open_market_ids.add(cid)
                        logger.info("Auto trader OPENED portfolio NO: %s (%d NOs, profit=%.2f%%)",
                                    pkg_name, len(no_targets), opp.get("profit_pct", 0))
                        if self.dlog:
                            self.dlog.log_trade_opened(
                                pkg_id=pkg.get("id", ""), title=pkg_name,
                                strategy="portfolio_no",
                                side="ALL_NO", price=round(total_no_cost, 4),
                                size=trade_size, score=score, spread_pct=spread_pct,
                                conviction=0.95,  # Near-guaranteed
                                days_to_expiry=days_to_expiry, volume=opp.get("volume", 0))
                except Exception as e:
                    logger.warning("Auto trader: portfolio NO trade failed: %s", e)
                    if self.dlog:
                        self.dlog.log_trade_failed(opp_title, str(e))
                continue

            # Weather forecast: single-leg directional bet based on NWS data
            if opp.get("opportunity_type") == "weather_forecast":
                side = opp.get("side", "YES")
                entry_price = opp.get("buy_yes_price", 0.5) if side == "YES" else opp.get("buy_no_price", 0.5)
                if entry_price <= 0:
                    continue

                try:
                    pkg = create_package(f"Auto: {trade_title[:60]}", "weather_forecast")
                except ValueError:
                    continue

                # Kelly-sized trade (Quarter Kelly — NWS data edge)
                trade_size = self._kelly_size("weather_forecast", directional_budget,
                                              implied_prob=entry_price,
                                              spread_pct=opp.get("edge", 0) * 100)
                if trade_size <= 0:
                    continue

                leg_type = "prediction_yes" if side == "YES" else "prediction_no"
                market_id = opp.get("market_ticker", opp.get("buy_yes_market_id", ""))

                pkg["legs"].append(create_leg(
                    platform="kalshi",
                    leg_type=leg_type,
                    asset_id=f"{market_id}:{side}",
                    asset_label=f"{side}: {opp.get('title', '?')[:40]}",
                    entry_price=entry_price,
                    cost=trade_size,
                    expiry=opp.get("expiry", opp.get("target_date", ""))[:10],
                ))

                # Daily weather markets resolve quickly — hold to resolution
                # NO stop loss — journal shows stop-losses destroy EV.
                pkg["exit_rules"].append(create_exit_rule("target_profit", {"target_pct": 30}))

                pkg["_use_limit_orders"] = True
                pkg["_use_brackets"] = True  # GTC target sell at 0% maker fee
                pkg_name = pkg.get("name", opp_title)
                try:
                    result = await self.pm.execute_package(pkg)
                    if result.get("success"):
                        trades_this_cycle += 1
                        self._trades_opened += 1
                        self._daily_trade_count += 1
                        directional_budget -= trade_size
                        total_exposure += trade_size
                        _cat = self._detect_category(opp_title)
                        category_exposure[_cat] = category_exposure.get(_cat, 0) + trade_size
                        cid = market_id.split(":")[0] if ":" in market_id else market_id
                        if cid:
                            open_market_ids.add(cid)
                        logger.info("Auto trader OPENED weather: %s (edge=%.1f%%, side=%s)",
                                    pkg_name, opp.get("edge", 0) * 100, side)
                        if self.dlog:
                            self.dlog.log_trade_opened(
                                pkg_id=pkg.get("id", ""), title=pkg_name,
                                strategy="weather_forecast",
                                side=side, price=entry_price,
                                size=trade_size, score=score, spread_pct=spread_pct,
                                conviction=min(1.0, opp.get("edge", 0) * 5),
                                days_to_expiry=days_to_expiry, volume=opp.get("volume", 0))
                except Exception as e:
                    logger.warning("Auto trader: weather trade failed: %s", e)
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

                # Kelly-sized trade (1/5 Kelly — LLM-derived edge)
                trade_size = self._kelly_size("political_synthetic", directional_budget,
                                              spread_pct=spread_pct)
                if trade_size <= 0:
                    continue

                for opp_leg in opp_legs:
                    leg_cost = round(trade_size * opp_leg.get("weight", 1.0 / len(opp_legs)), 2)
                    leg_cost = max(self._min_trade_size, leg_cost)
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
                # NO stop loss or trailing stop — journal shows both destroy EV.
                pkg["_use_brackets"] = True  # GTC target sell at 0% maker fee
                pkg["_political_strategy"] = opp.get("strategy", {})

                # Political packages skip normal strategy/side determination.
                # Fall through to the execution block below (try/await pm.execute_package).
                if not pkg["legs"]:
                    self._record_skip("political_no_legs")
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
                        directional_budget -= trade_size
                        total_exposure += trade_size
                        _cat = self._detect_category(opp_title)
                        category_exposure[_cat] = category_exposure.get(_cat, 0) + trade_size
                        # Refresh open IDs for this cycle
                        for leg in pkg.get("legs", []):
                            cid = leg.get("asset_id", "").split(":")[0]
                            if cid:
                                open_market_ids.add(cid)
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

            # Crypto synthetic: same structure as political_synthetic, different exit rules
            if opp.get("opportunity_type") == "crypto_synthetic":
                try:
                    pkg = create_package(f"Auto: {trade_title[:60]}", "crypto_synthetic")
                except ValueError:
                    continue

                opp_legs = opp.get("legs", [])
                if not opp_legs:
                    continue

                # Kelly-sized trade (1/5 Kelly — LLM-derived edge)
                trade_size = self._kelly_size("crypto_synthetic", directional_budget,
                                              spread_pct=spread_pct)
                if trade_size <= 0:
                    continue

                for opp_leg in opp_legs:
                    leg_cost = round(trade_size * opp_leg.get("weight", 1.0 / len(opp_legs)), 2)
                    leg_cost = max(self._min_trade_size, leg_cost)
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
                # NO stop loss or trailing stop — journal shows both destroy EV.
                pkg["_use_brackets"] = True

                if not pkg["legs"]:
                    self._record_skip("crypto_synthetic_no_legs")
                    continue

                pkg["_use_limit_orders"] = True
                pkg_name = pkg.get("name", opp_title)
                bet_side = "CRYPTO"
                bet_conviction = 0.0
                entry_price = 0.5
                try:
                    result = await self.pm.execute_package(pkg)
                    if result.get("success"):
                        trades_this_cycle += 1
                        self._trades_opened += 1
                        self._daily_trade_count += 1
                        directional_budget -= trade_size
                        total_exposure += trade_size
                        _cat = self._detect_category(opp_title)
                        category_exposure[_cat] = category_exposure.get(_cat, 0) + trade_size
                        for leg in pkg.get("legs", []):
                            cid = leg.get("asset_id", "").split(":")[0]
                            if cid:
                                open_market_ids.add(cid)
                        logger.info("Auto trader OPENED crypto synthetic: %s (ev=%.1f%%, size=$%.2f)",
                                    pkg_name, spread_pct, trade_size)
                        if self.dlog:
                            self.dlog.log_trade_opened(
                                pkg_id=pkg.get("id", ""), title=pkg_name,
                                strategy="crypto_synthetic",
                                side=bet_side, price=entry_price,
                                size=trade_size, score=score, spread_pct=spread_pct,
                                conviction=bet_conviction,
                                days_to_expiry=days_to_expiry, volume=opp.get("volume", 0))
                except Exception as e:
                    logger.warning("Auto trader: crypto trade failed: %s", e)
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
                # create the edge, not platform differences.
                # Gate: require high EV confidence before entering synthetics.
                # Journal: synthetic_derivative with low-confidence legs lost -$33.22.
                # The arb engine already rejects loss_prob > 0.60, but that's too loose —
                # prediction markets have noisy pricing, so require tighter thresholds.
                synth_info = opp.get("synthetic_info", {})
                synth_loss_prob = synth_info.get("loss_probability", 0.5)

                # Compute true probability-weighted EV from scenario data.
                # The arb engine's profit_pct uses spread*(1-loss_prob) which is
                # "probability-adjusted spread % of payout" — not the true expected
                # return on capital. Use scenarios for accurate EV.
                scenarios = synth_info.get("scenarios", {})
                total_cost = synth_info.get("total_cost", 0)
                if scenarios and total_cost > 0:
                    # Conservative EV: use MINIMUM win return, not uniform average.
                    # Zone probabilities aren't known per-zone — the arb engine only
                    # computes aggregate loss_probability. Uniform weighting inflates
                    # EV because narrow "bonus zones" (e.g., $48 range at 785%)
                    # get the same weight as wide zones ($1000+ range at 342%).
                    # Using min(win returns) is conservative: assumes you'll land in
                    # the worst winning zone, which is also usually the widest.
                    win_returns = [s["return_pct"] for s in scenarios.values() if s["return_pct"] > 0]
                    loss_returns = [s["return_pct"] for s in scenarios.values() if s["return_pct"] <= 0]
                    if win_returns and loss_returns:
                        min_win = min(win_returns)
                        avg_loss = sum(loss_returns) / len(loss_returns)
                        synth_ev = (1.0 - synth_loss_prob) * min_win + synth_loss_prob * avg_loss
                    else:
                        synth_ev = opp.get("profit_pct", 0)
                else:
                    synth_ev = opp.get("profit_pct", 0)

                if synth_loss_prob >= 0.20:
                    self._record_skip("synthetic_high_loss_prob")
                    if self.dlog:
                        self.dlog.log_opportunity_skip(opp_title, "synthetic_high_loss_prob",
                                                       loss_prob=round(synth_loss_prob, 3),
                                                       ev_pct=round(synth_ev, 2),
                                                       volume=opp_volume, strategy=opp_strategy)
                    continue
                if synth_ev < 15.0:
                    self._record_skip("synthetic_low_ev")
                    if self.dlog:
                        self.dlog.log_opportunity_skip(opp_title, "synthetic_low_ev",
                                                       ev_pct=round(synth_ev, 2),
                                                       loss_prob=round(synth_loss_prob, 3),
                                                       volume=opp_volume, strategy=opp_strategy)
                    continue
                strategy = "synthetic_derivative"
            elif is_cross_platform:
                strategy = "cross_platform_arb"
            else:
                strategy = "pure_prediction"

            try:
                pkg = create_package(f"Auto: {trade_title[:60]}", strategy)
            except ValueError:
                pkg = create_package(f"Auto: {trade_title[:60]}", "pure_prediction")

            # Propagate signal flags so position_manager dedup guard allows re-entry
            if opp.get("_news_driven"):
                pkg["_news_driven"] = True
            if opp.get("insider_signal"):
                pkg["insider_signal"] = opp["insider_signal"]

            if is_cross_platform or is_synthetic:
                # Kelly size for arb/synthetic strategies
                # Cross-platform arb uses reserved arb budget; synthetics use directional budget
                _strategy_budget = arb_budget if is_cross_platform else directional_budget
                trade_size = self._kelly_size(strategy, _strategy_budget,
                                              spread_pct=spread_pct,
                                              bypass_regime=is_cross_platform)
                if trade_size <= 0:
                    continue
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
                    self._record_skip("zero_price_multi_leg")
                    if self.dlog:
                        self.dlog.log_opportunity_skip(opp_title, "zero_price_multi_leg",
                                                       yes_price=round(buy_yes_price, 4),
                                                       no_price=round(buy_no_price, 4),
                                                       volume=opp_volume, strategy=opp_strategy)
                    continue

                # Need market IDs for both legs
                if not yes_market_id or not no_market_id:
                    self._record_skip("missing_market_id_multi_leg")
                    if self.dlog:
                        self.dlog.log_opportunity_skip(opp_title, "missing_market_id_multi_leg",
                                                       yes_id=yes_market_id[:12] if yes_market_id else "",
                                                       no_id=no_market_id[:12] if no_market_id else "",
                                                       volume=opp_volume, strategy=opp_strategy)
                    continue

                # For synthetics, allocate equal capital to each leg.
                # Proportional sizing (old) put 88% into the expensive leg and 12%
                # into the cheap hedge — when the expensive leg lost, the hedge was
                # too small to matter.  Equal sizing ensures both legs are meaningful.
                # The payoff is structural (one leg wins at resolution), not directional.
                if is_synthetic:
                    synth = opp.get("synthetic_info", {})
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
                    side, side_price, side_id = "NO", buy_no_price, no_market_id
                    leg_type = "prediction_no"
                elif buy_yes_price >= 0.50 and yes_market_id:
                    # Slight YES lean
                    side, side_price, side_id = "YES", buy_yes_price, yes_market_id
                    leg_type = "prediction_yes"
                elif no_market_id:
                    # Slight NO lean
                    side, side_price, side_id = "NO", buy_no_price, no_market_id
                    leg_type = "prediction_no"
                elif yes_market_id:
                    side, side_price, side_id = "YES", buy_yes_price, yes_market_id
                    leg_type = "prediction_yes"
                else:
                    self._record_skip("no_valid_side")
                    continue

                # Skip extreme OTM (lottery tickets) — entry < 0.15 loses 85%+ of the time
                # Allow high-probability entries (> 0.85) — they resolve at $1.00
                # Only skip truly extreme entries (> 0.96) where fees exceed max profit
                if side_price < 0.15:
                    self._record_skip("extreme_otm")
                    if self.dlog:
                        self.dlog.log_opportunity_skip(opp_title, "extreme_otm",
                                                       side=side, price=round(side_price, 4),
                                                       volume=opp_volume, strategy=opp_strategy)
                    continue
                if side_price > 0.96:
                    self._record_skip("extreme_itm")
                    if self.dlog:
                        self.dlog.log_opportunity_skip(opp_title, "extreme_itm",
                                                       side=side, price=round(side_price, 4),
                                                       volume=opp_volume, strategy=opp_strategy)
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
                # Variable Kelly fraction: conservative on longshots, standard on favorites
                if side_price <= 0.30:
                    kelly_frac = 0.125  # 1/8 Kelly for longshots (high uncertainty)
                elif side_price >= 0.70:
                    kelly_frac = 0.25   # 1/4 Kelly for favorites (more confident)
                else:
                    kelly_frac = 0.20   # 1/5 Kelly for mid-range
                kelly_quarter = max(0.0, kelly_full * kelly_frac)

                # Bad regime: skip speculative directional bets entirely
                if self._regime_penalty < 1.0:
                    if self.dlog:
                        self.dlog.log_opportunity_skip(opp_title, "bad_regime",
                                                       volume=opp_volume, strategy=opp_strategy)
                    continue

                # Apply Kelly fraction to directional budget, capped at _max_trade_size
                sized_trade = round(min(self._max_trade_size, directional_budget * kelly_quarter), 2)
                sized_trade = max(self._min_trade_size, min(sized_trade, trade_size))

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
                self._record_skip("no_legs_after_sizing")
                continue

            # Exit rules — strategy-dependent
            if strategy == "cross_platform_arb":
                # Cross-platform arb: guaranteed profit at resolution — HOLD TO RESOLUTION
                # The spread is locked in at entry; exiting early forfeits the guarantee
                # NO stop loss — journal shows stop-losses destroy EV on prediction markets.
                # These positions resolve at $0 or $1; temporary drawdowns are noise.
                # No trailing stop, no time decay — these resolve at $0 or $1
                pkg["_hold_to_resolution"] = True
                pkg["_min_hold_until"] = time.time() + 86400
            elif strategy == "synthetic_derivative":
                # Synthetics: structural edge from different strike prices — HOLD TO RESOLUTION
                # The payoff depends on where the underlying lands relative to strikes
                # NO stop loss — journal shows stop-losses destroy EV; synthetics need room.
                # High-EV gate at entry (loss_prob <= 25%, EV >= 15%) is the protection.
                pkg["exit_rules"].append(create_exit_rule("target_profit", {"target_pct": 80}))
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
                    # NO stop loss — journal shows stop-losses destroy EV on prediction
                    # markets. These resolve at $0 or $1; temporary drawdowns are noise.
                    # No trailing stop — these should resolve, not be scalped
                    pkg["_hold_to_resolution"] = True
                    pkg["_min_hold_until"] = time.time() + 86400
                else:
                    # Standard prediction — tuned from journal analysis
                    pkg["exit_rules"].append(create_exit_rule("target_profit", {"target_pct": 50}))
                    # NO stop loss — journal shows stop-losses destroy EV.
                    # Prediction markets resolve at $0 or $1; the target_profit exit
                    # captures upside, and resolution handles the rest.
                    # No trailing stop — 0/8 trailing stop wins in journal.
                    pkg["_min_hold_until"] = time.time() + 86400

            # Hold to resolution for short-expiry prediction markets.
            if days_to_expiry <= HOLD_TO_RESOLUTION_MAX_DAYS:
                pkg["_hold_to_resolution"] = True
            # Also hold favorites (>$0.85) regardless of expiry.
            # For pure_prediction, side_price is the directional entry price.
            # For cross_platform_arb/synthetic, those are already hold-to-resolution
            # above, but we check buy_yes_price as a reasonable proxy.
            _entry_for_favorite_check = (
                side_price if strategy == "pure_prediction" else buy_yes_price
            )
            if _entry_for_favorite_check >= 0.85:
                pkg["_hold_to_resolution"] = True

            # Use limit orders for 0% maker fees on entry
            pkg["_use_limit_orders"] = True
            pkg["_category"] = self._detect_category(opp_title)
            if not pkg.get("_hold_to_resolution"):
                pkg["_use_brackets"] = True

            # Execute
            pkg_name = pkg.get("name", opp_title)
            bet_side = pkg.get("_bet_side", "SYNTHETIC" if is_synthetic else ("BOTH" if is_cross_platform else "?"))
            bet_conviction = pkg.get("_entry_conviction", round(abs(buy_yes_price - 0.5), 3))
            entry_price = side_price if strategy == "pure_prediction" else buy_yes_price
            try:
                result = await self.pm.execute_package(pkg)
                if result.get("success"):
                    trades_this_cycle += 1
                    self._trades_opened += 1
                    # News-driven trades have their own daily cap (DAILY_TRADE_CAP=5 in news_scanner).
                    # Don't count them against the auto_trader's 3/day arb limit — otherwise
                    # arb trades consume the cap before news signals can execute.
                    if not opp.get("_news_driven"):
                        self._daily_trade_count += 1
                    # Decrement appropriate budget based on strategy type
                    if is_cross_platform:
                        arb_budget -= trade_size
                    else:
                        directional_budget -= trade_size
                    total_exposure += trade_size
                    # Hard stop if exposure cap reached mid-cycle
                    if total_exposure >= self._max_total_exposure:
                        remaining_slots = 0
                    cat = self._detect_category(opp_title)
                    category_exposure[cat] = category_exposure.get(cat, 0) + trade_size
                    # Refresh open market IDs so later iterations in this cycle see this trade
                    if yes_mid:
                        open_market_ids.add(yes_mid)
                    if no_mid:
                        open_market_ids.add(no_mid)
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
                            score_metadata=score_metadata,
                        )
                else:
                    self._record_skip("execution_failed")
                    logger.warning("Auto trader: execution failed for %s: %s", pkg_name, result.get("error"))
                    if self.dlog:
                        self.dlog.log_trade_failed(pkg_name, result.get("error", "unknown"))
            except Exception as e:
                self._record_skip("execution_exception")
                logger.error("Auto trader: exception creating package: %s", e)
                if self.dlog:
                    self.dlog.log_trade_failed(pkg_name, str(e))

        if trades_this_cycle > 0:
            logger.info("Auto trader: opened %d new positions this cycle", trades_this_cycle)

        if insider_only_mode and self.dlog:
            self.dlog.log_scan_summary(
                "insider_mode_filter",
                passed=insider_mode_passed_filter_count,
                skipped=insider_mode_skipped_filter_count
            )

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

        # Skip opportunities on platforms we can't trade on
        # Same pattern as _events_to_opportunities() — falls back to {"polymarket"} if pm is None
        tradeable = set(self.pm.executors.keys()) if self.pm else {"polymarket"}
        if buy_yes_platform not in tradeable or buy_no_platform not in tradeable:
            logger.debug("Skipping arb on non-tradeable platform: %s/%s",
                          buy_yes_platform, buy_no_platform)
            return None

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
        # Cross-platform fees: Polymarket maker 0% + Kalshi ~1.2% = ~1.2% round-trip
        profit_pct = arb.get("profit_pct", 0)
        is_cross_platform = buy_yes_platform != buy_no_platform
        if is_cross_platform:
            min_spread = 2.0  # Must exceed combined cross-platform fees (0% Poly + 1.2% Kalshi)
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
            "net_profit_pct": arb.get("net_profit_pct", profit_pct),
            "opportunity_type": "cross_platform_arb" if is_cross_platform else "synthetic_derivative",
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

                # Same filters as main loop — allow favorites up to 0.96
                if yes_price > 0.96 or yes_price < 0.05:
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

                # Favorite-longshot bias (consistent with main scoring)
                if favored_price >= 0.80:
                    score *= 2.5
                elif favored_price >= 0.70:
                    score *= 1.8
                elif favored_price <= 0.20:
                    score *= 0.2
                elif favored_price <= 0.30:
                    score *= 0.5

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

                    # Skip extremes — allow favorites up to 0.96 per main loop thresholds
                    if yes_price > 0.96 or yes_price < 0.05:
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

            # Favorite-longshot bias
            favored_price = min(opp["buy_yes_price"], opp["buy_no_price"])
            if favored_price >= 0.80:
                score *= 2.5
            elif favored_price >= 0.70:
                score *= 1.8
            elif favored_price <= 0.20:
                score *= 0.2
            elif favored_price <= 0.30:
                score *= 0.5

            opp["_score"] = score

        opportunities.sort(key=lambda o: o.get("_score", 0), reverse=True)
        logger.info("Auto trader: found %d markets on Polymarket (top score=%.1f)",
                     len(opportunities), opportunities[0]["_score"] if opportunities else 0)
        return opportunities[:10]  # Top 10 across all categories

    def _record_skip(self, reason: str):
        """Increment skip counter and track reason breakdown."""
        self._trades_skipped += 1
        self._skip_reasons[reason] = self._skip_reasons.get(reason, 0) + 1

    def get_stats(self) -> dict:
        open_pkgs = self.pm.list_packages("open")
        return {
            "running": self._running,
            "trades_opened": self._trades_opened,
            "trades_skipped": self._trades_skipped,
            "open_positions": len(open_pkgs),
            "total_exposure": round(sum(p.get("total_cost", 0) for p in open_pkgs), 2),
            "max_exposure": self._max_total_exposure,
            "scan_interval_sec": self.interval,
            "trades_today": self._daily_trade_count,
            "max_trades_per_day": MAX_NEW_TRADES_PER_DAY,
            "skip_reasons": dict(sorted(self._skip_reasons.items(), key=lambda x: x[1], reverse=True)),
        }
