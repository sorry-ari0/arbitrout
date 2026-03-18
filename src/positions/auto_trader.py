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
MIN_TRADE_SIZE = 25.0        # Min $25 per trade
MAX_CONCURRENT = 10          # Max 10 open packages
MAX_TOTAL_EXPOSURE = 2000.0  # Max $2000 total
SCAN_INTERVAL = 300          # 5 minutes between scans
MIN_SPREAD_PCT = 5.0         # Minimum 5% spread (must exceed ~4% round-trip fees)


class AutoTrader:
    """Autonomous paper trader that creates packages from scanner opportunities."""

    def __init__(self, position_manager, scanner=None, interval: float = SCAN_INTERVAL):
        self.pm = position_manager
        self.scanner = scanner
        self.interval = interval
        self._task = None
        self._running = False
        self._trades_opened = 0
        self._trades_skipped = 0

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

    async def _loop(self):
        await asyncio.sleep(10)  # Let server fully start
        while self._running:
            try:
                await self._scan_and_trade()
            except Exception as e:
                logger.error("Auto trader scan error: %s", e)
            await asyncio.sleep(self.interval)

    async def _scan_and_trade(self):
        """One scan cycle: find opportunities, filter, create packages."""
        open_pkgs = self.pm.list_packages("open")
        if len(open_pkgs) >= MAX_CONCURRENT:
            logger.info("Auto trader: at max concurrent positions (%d), skipping", len(open_pkgs))
            return

        total_exposure = sum(p.get("total_cost", 0) for p in open_pkgs)
        if total_exposure >= MAX_TOTAL_EXPOSURE:
            logger.info("Auto trader: at max exposure ($%.2f), skipping", total_exposure)
            return

        remaining_budget = MAX_TOTAL_EXPOSURE - total_exposure
        remaining_slots = MAX_CONCURRENT - len(open_pkgs)

        # Scan for opportunities — use scanner if available, else query Polymarket directly
        opportunities = []
        if self.scanner:
            try:
                result = await self.scanner.scan()
                opportunities = result.get("opportunities", [])
            except Exception as e:
                logger.warning("Auto trader: scanner failed: %s", e)

        if not opportunities:
            # Direct Polymarket scan for crypto markets
            opportunities = await self._scan_polymarket_crypto()

        if not opportunities:
            logger.info("Auto trader: no opportunities found this cycle")
            return

        logger.info("Auto trader: found %d opportunities, budget=$%.2f, slots=%d",
                     len(opportunities), remaining_budget, remaining_slots)

        trades_this_cycle = 0
        for opp in opportunities:
            if trades_this_cycle >= remaining_slots:
                break
            if remaining_budget < MIN_TRADE_SIZE:
                break

            # Filter: require minimum spread
            spread_pct = opp.get("profit_pct", 0)
            if spread_pct < MIN_SPREAD_PCT:
                continue

            # Prioritize crypto-related and near-expiry
            title = (opp.get("title") or opp.get("canonical_title") or "").lower()
            is_crypto = any(kw in title for kw in ["btc", "bitcoin", "eth", "ethereum", "crypto", "solana", "sol", "xrp"])

            # Check expiry
            expiry = opp.get("expiry") or opp.get("end_date") or ""
            is_near_expiry = False
            if expiry:
                try:
                    exp_date = datetime.strptime(expiry[:10], "%Y-%m-%d").date()
                    days_to_expiry = (exp_date - date.today()).days
                    is_near_expiry = 0 < days_to_expiry <= 30
                except (ValueError, TypeError):
                    pass

            # Score: crypto near-expiry > crypto > near-expiry > other
            score = spread_pct
            if is_crypto:
                score *= 2.0
            if is_near_expiry:
                score *= 1.5

            # Skip low-score opportunities
            if score < MIN_SPREAD_PCT:
                self._trades_skipped += 1
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
                continue

            # Create the package
            from .position_manager import create_package, create_leg, create_exit_rule

            trade_title = opp.get("title") or opp.get("canonical_title") or f"Auto-{int(time.time())}"
            pkg_name = f"Auto: {trade_title[:60]}"

            # Determine strategy type
            if buy_yes_platform != buy_no_platform:
                strategy = "cross_platform_arb"
            else:
                strategy = "pure_prediction"

            try:
                pkg = create_package(pkg_name, strategy)
            except ValueError:
                pkg = create_package(pkg_name, "pure_prediction")

            # Split cost between legs
            half = round(trade_size / 2, 2)

            if yes_market_id:
                yes_leg = create_leg(
                    platform=buy_yes_platform,
                    leg_type="prediction_yes",
                    asset_id=f"{yes_market_id}:YES",
                    asset_label=f"YES @ {buy_yes_platform}",
                    entry_price=buy_yes_price if buy_yes_price > 0 else 0.5,
                    cost=half,
                    expiry=expiry[:10] if expiry else "2026-12-31",
                )
                pkg["legs"].append(yes_leg)

            if no_market_id:
                no_leg = create_leg(
                    platform=buy_no_platform,
                    leg_type="prediction_no",
                    asset_id=f"{no_market_id}:NO",
                    asset_label=f"NO @ {buy_no_platform}",
                    entry_price=buy_no_price if buy_no_price > 0 else 0.5,
                    cost=half,
                    expiry=expiry[:10] if expiry else "2026-12-31",
                )
                pkg["legs"].append(no_leg)

            if not pkg["legs"]:
                self._trades_skipped += 1
                continue

            # Add exit rules
            pkg["exit_rules"].append(create_exit_rule("target_profit", {"target_pct": 15}))
            pkg["exit_rules"].append(create_exit_rule("stop_loss", {"stop_pct": -10}))
            pkg["exit_rules"].append(create_exit_rule("trailing_stop", {"current": 8, "bound_min": 3, "bound_max": 20}))

            # Execute
            try:
                result = await self.pm.execute_package(pkg)
                if result.get("success"):
                    trades_this_cycle += 1
                    self._trades_opened += 1
                    remaining_budget -= trade_size
                    logger.info("Auto trader OPENED: %s (spread=%.1f%%, size=$%.2f, score=%.1f)",
                                pkg_name, spread_pct, trade_size, score)
                else:
                    self._trades_skipped += 1
                    logger.warning("Auto trader: execution failed for %s: %s", pkg_name, result.get("error"))
            except Exception as e:
                self._trades_skipped += 1
                logger.error("Auto trader: exception creating package: %s", e)

        if trades_this_cycle > 0:
            logger.info("Auto trader: opened %d new positions this cycle", trades_this_cycle)

    async def _scan_polymarket_crypto(self) -> list[dict]:
        """Direct scan of Polymarket Gamma API for crypto prediction markets."""
        if not httpx:
            return []

        GAMMA_API = "https://gamma-api.polymarket.com"
        crypto_keywords = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto", "xrp", "doge"]
        opportunities = []

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Fetch high-volume active markets in bulk, then filter for crypto
                seen_ids = set()
                all_markets = []
                for offset in [0, 50]:
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

                logger.info("Auto trader: fetched %d markets from Polymarket", len(all_markets))

                for market in all_markets:
                    question = (market.get("question") or "").lower()
                    # Filter: must contain crypto keyword
                    if not any(kw in question for kw in crypto_keywords):
                        continue

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

                    # Skip if too close to resolved (>0.95 or <0.05)
                    if yes_price > 0.95 or yes_price < 0.05:
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

                    # Score based on conviction (distance from 0.5)
                    conviction = abs(yes_price - 0.5)
                    volume = float(market.get("volumeNum", 0) or market.get("volume", 0) or 0)

                    # Profit potential: how much can be gained if market resolves in the favored direction
                    profit_potential = max(1.0 - yes_price, yes_price) - 0.5

                    opp = {
                        "title": title,
                        "canonical_title": title,
                        "buy_yes_platform": "polymarket",
                        "buy_yes_price": yes_price,
                        "buy_no_platform": "polymarket",
                        "buy_no_price": no_price,
                        "buy_yes_market_id": mid,
                        "buy_no_market_id": mid,
                        "profit_pct": round(profit_potential * 100, 1),
                        "expiry": end_date[:10] if end_date else "",
                        "days_to_expiry": days_to_expiry,
                        "volume": volume,
                        "conviction": round(conviction, 3),
                    }
                    opportunities.append(opp)

        except Exception as e:
            logger.warning("Auto trader: Polymarket scan failed: %s", e)

        # Sort by conviction * near-expiry bonus
        for opp in opportunities:
            score = opp["profit_pct"]
            if opp.get("days_to_expiry", 999) <= 30:
                score *= 1.5
            if opp.get("days_to_expiry", 999) <= 7:
                score *= 2.0
            if opp.get("volume", 0) > 10000:
                score *= 1.2
            opp["_score"] = score

        opportunities.sort(key=lambda o: o.get("_score", 0), reverse=True)
        logger.info("Auto trader: found %d crypto markets on Polymarket", len(opportunities))
        return opportunities[:10]  # Top 10

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
        }
