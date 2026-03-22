"""Insider/whale tracker — monitors top Polymarket traders and their positions.

Uses Polymarket's public Data API to:
1. Fetch top traders from the leaderboard (by PNL)
2. Track their current positions on specific markets
3. Detect suspicious patterns (high win rates, pre-resolution trades)
4. Provide signals to the auto trader for position scoring

Data API: https://data-api.polymarket.com (no auth required)
"""
import asyncio
import json
import logging
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger("positions.insider_tracker")

DATA_API = "https://data-api.polymarket.com"

# Thresholds
MIN_PNL_USD = 10000          # Only track traders with >$10K lifetime PNL
MIN_WIN_RATE = 0.65          # Flag traders with >65% win rate
SUSPICIOUS_WIN_RATE = 0.80   # Highly suspicious above 80%
MIN_POSITION_SIZE = 100      # Minimum $100 position to count as signal
LEADERBOARD_SIZE = 50        # Top 50 traders to monitor
SCAN_INTERVAL = 900          # 15 minutes between full scans
CACHE_TTL = 600              # Cache insider data for 10 minutes
CONVERGENCE_THRESHOLD = 3    # 3+ wallets entering same market = convergence signal

# Wallet classification thresholds
# Conviction = consistently winning at high rates, regardless of volume
ROI_CONVICTION_MIN = 0.15    # >15% ROI = conviction trader (consistent winners)
ROI_MARKET_MAKER_MAX = 0.05  # <5% ROI = market maker (spread capture, noise)
VOL_MARKET_MAKER_MIN = 100_000_000  # >$100M volume + low ROI = market maker

# High-conviction watchlist: known profitable directional traders
# These get 5x signal weight and bypass normal ROI thresholds
# Source: Polymarket leaderboard analysis (2026-03-20)
HIGH_CONVICTION_WATCHLIST = {
    "0x56687bf447db6ffa42ffe2204a05edaa20f55839": "Theo4",        # $22M PNL, 51% ROI
    "0x1f2dd6d473f3e824cd2f8a89d9c69fb96f6ad0cf": "Fredi9999",    # $16.6M PNL, 22% ROI
    "0x863134d00841b2e200492805a01e1e2f5defaa53": "RepTrump",     # $7.5M PNL, 54% ROI
    "0x78b9ac44a6d7d7a076c14e0ad518b301b63c6b76": "Len9311238",   # $8.7M PNL, 53% ROI
    "0x885783a5e42d297c3532081ebf5c14ba0e9b0a44": "BetTom42",     # $5.6M PNL, 50% ROI
    "0x23786fdd7bf5a6fa7c249a3b tried1ff4c6b2f01": "mikatrade77",  # $5.2M PNL, 47% ROI
    "0xd0c042f8ac8f16a957f75de8c2e1e64e30e625c1": "alexmulti",    # $4.8M PNL, 48% ROI
    "0x16f91d4d0c17c5de07d2f01bceac542c4e4a05a8": "Jenzigo",      # $4.1M PNL, 43% ROI
}

# Known market makers (high volume, low ROI — exclude from directional signals)
KNOWN_MARKET_MAKERS = {
    "0x204f72f35326db932158cba6adff0b9a1da95e14": "swisstony",    # $5.4M PNL, 0.96% ROI, $562M vol
    "0x2005d16a84ceefa912d4e380cd32e7ff827875ea": "RN1",          # $5.8M PNL, 2.1% ROI, $283M vol
    "0xe90bec87d9ef430f27f9dcfe72c34b76967d5da2": "gmanas",       # $5.0M PNL, 0.94% ROI, $529M vol
    "0x507e52ef684ca2dd91f90a9d26d149dd3288beae": "GamblingIsAllYouNeed",  # $4.4M, 1.6%, $268M
}


class InsiderTracker:
    """Tracks whale/insider activity on Polymarket for trading signals."""

    def __init__(self, data_dir: Path, auto_trader=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.auto_trader = auto_trader  # Set after init to avoid circular
        self._top_traders: list[dict] = []
        self._insider_positions: dict[str, list[dict]] = {}  # condition_id -> positions
        self._prev_positions: dict[str, list[dict]] = {}  # Previous scan for movement detection
        self._flagged_wallets: dict[str, dict] = {}  # wallet -> trader info
        self._wallet_accuracy: dict[str, dict] = {}  # wallet -> {correct, total, accuracy}
        self._movement_alerts: list[dict] = []  # Recent significant movements
        self._last_scan: float = 0
        self._task = None
        self._running = False
        # Conviction watchlist: start with hardcoded defaults, then override from disk
        self._conviction_watchlist: dict[str, str] = dict(HIGH_CONVICTION_WATCHLIST)
        self._load_watchlist()
        self._load_cache()

    def _load_watchlist(self):
        """Load refreshed watchlist from disk, falling back to hardcoded."""
        path = self.data_dir / "conviction_watchlist.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("wallets") and data.get("updated_at", 0) > 0:
                    self._conviction_watchlist = data["wallets"]
                    logger.info("Loaded refreshed watchlist: %d wallets (updated %s)",
                                len(self._conviction_watchlist),
                                data.get("updated_at_str", "unknown"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load watchlist: %s", e)

    def update_watchlist(self, wallets: dict[str, str]):
        """Update the conviction watchlist and persist to disk.

        wallets: {address: display_name} dict
        Merges with hardcoded defaults — never shrinks below the base set.
        """
        merged = dict(HIGH_CONVICTION_WATCHLIST)
        merged.update(wallets)
        self._conviction_watchlist = merged

        path = self.data_dir / "conviction_watchlist.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "wallets": merged,
            "updated_at": time.time(),
            "updated_at_str": time.strftime("%Y-%m-%d %H:%M"),
        }
        with open(str(path), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Updated conviction watchlist: %d wallets", len(merged))

    def _load_cache(self):
        """Load cached insider data from disk."""
        path = self.data_dir / "insider_signals.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._top_traders = data.get("top_traders", [])
                self._insider_positions = data.get("insider_positions", {})
                self._prev_positions = data.get("prev_positions", {})
                self._flagged_wallets = data.get("flagged_wallets", {})
                self._wallet_accuracy = data.get("wallet_accuracy", {})
                self._movement_alerts = data.get("movement_alerts", [])
                self._last_scan = data.get("last_scan", 0)
                logger.info("Loaded %d tracked insiders from cache", len(self._flagged_wallets))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load insider cache: %s", e)

    def _save_cache(self):
        """Persist insider data to disk."""
        path = self.data_dir / "insider_signals.json"
        tmp = str(path) + ".tmp"
        import os
        data = {
            "top_traders": self._top_traders,
            "insider_positions": self._insider_positions,
            "prev_positions": self._prev_positions,
            "flagged_wallets": self._flagged_wallets,
            "wallet_accuracy": self._wallet_accuracy,
            "movement_alerts": self._movement_alerts[-100:],  # Keep last 100 alerts
            "last_scan": self._last_scan,
            "saved_at": time.time(),
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(path))

    def start(self):
        """Start background scanning loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info("Insider tracker started (interval=%ds)", SCAN_INTERVAL)

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Insider tracker stopped")

    async def _loop(self):
        await asyncio.sleep(30)  # Let server start
        while self._running:
            try:
                await self.scan()
            except Exception as e:
                logger.error("Insider tracker scan error: %s", e)
            await asyncio.sleep(SCAN_INTERVAL)

    async def scan(self):
        """Full scan cycle: fetch leaderboard, get positions, detect movements, flag insiders."""
        if not httpx:
            logger.warning("httpx not available for insider tracking")
            return

        # Save previous positions for movement detection
        self._prev_positions = dict(self._insider_positions)

        async with httpx.AsyncClient(timeout=20.0) as client:
            # Step 1: Fetch top traders by PNL
            await self._fetch_leaderboard(client)

            # Step 2: For flagged wallets, fetch their current positions
            await self._fetch_insider_positions(client)

        # Step 3: Detect significant movements (new positions, exits, size changes)
        self._detect_movements()

        self._last_scan = time.time()
        self._save_cache()
        logger.info("Insider scan: %d tracked, %d flagged, %d signals, %d movement alerts",
                     len(self._top_traders), len(self._flagged_wallets),
                     len(self._insider_positions), len(self._movement_alerts))

    async def _fetch_leaderboard(self, client: "httpx.AsyncClient"):
        """Fetch top traders from Polymarket leaderboard."""
        traders = []
        for category in ["OVERALL", "CRYPTO"]:
            for time_period in ["ALL", "MONTH"]:
                try:
                    r = await client.get(f"{DATA_API}/v1/leaderboard", params={
                        "category": category,
                        "timePeriod": time_period,
                        "orderBy": "PNL",
                        "limit": str(LEADERBOARD_SIZE),
                    })
                    if r.status_code == 200:
                        data = r.json()
                        if isinstance(data, list):
                            traders.extend(data)
                        elif isinstance(data, dict):
                            # Some endpoints wrap in an object
                            traders.extend(data.get("leaderboard", data.get("data", [])))
                except Exception as e:
                    logger.warning("Leaderboard fetch failed (%s/%s): %s", category, time_period, e)
                await asyncio.sleep(0.5)

        # Deduplicate by wallet address
        seen = set()
        unique_traders = []
        for t in traders:
            wallet = t.get("proxyWallet", "")
            if wallet and wallet not in seen:
                seen.add(wallet)
                unique_traders.append(t)

        self._top_traders = unique_traders[:LEADERBOARD_SIZE * 2]  # Keep top 100

        # Flag wallets with high PNL or suspicious patterns
        # Classify into conviction traders vs market makers
        for t in self._top_traders:
            wallet = t.get("proxyWallet", "")
            pnl = float(t.get("pnl", 0) or 0)
            volume = float(t.get("vol", 0) or 0)

            if pnl < MIN_PNL_USD:
                continue

            # Estimate win rate from PNL/volume ratio
            roi = pnl / volume if volume > 0 else 0
            is_suspicious = roi > 0.12  # >12% ROI is noteworthy (consistent winners)

            # Classify wallet type
            # Priority: watchlist > known MM > ROI-based classification
            # Conviction = consistently winning at high rates (ROI), regardless of volume
            if wallet.lower() in {k.lower() for k in self._conviction_watchlist}:
                wallet_type = "conviction"
                signal_weight = 5.0  # Watchlist wallets get 5x weight
            elif wallet.lower() in {k.lower() for k in KNOWN_MARKET_MAKERS}:
                wallet_type = "market_maker"
                signal_weight = 0.0  # Market makers excluded from directional signals
            elif roi >= ROI_CONVICTION_MIN:
                # High win rate = conviction, no volume gate
                wallet_type = "conviction"
                signal_weight = 3.0 + min(roi * 5, 2.0)  # 3-5x weight, scales with ROI
            elif roi <= ROI_MARKET_MAKER_MAX and volume >= VOL_MARKET_MAKER_MIN:
                wallet_type = "market_maker"
                signal_weight = 0.0  # Low ROI, high volume = spread capture
            else:
                wallet_type = "unknown"
                signal_weight = 1.0

            self._flagged_wallets[wallet] = {
                "wallet": wallet,
                "username": t.get("userName", ""),
                "pnl": round(pnl, 2),
                "volume": round(volume, 2),
                "roi_pct": round(roi * 100, 2),
                "rank": t.get("rank", "?"),
                "suspicious": is_suspicious,
                "wallet_type": wallet_type,
                "signal_weight": signal_weight,
                "flagged_at": time.time(),
                "x_username": t.get("xUsername", ""),
                "verified": t.get("verifiedBadge", False),
            }

        logger.info("Leaderboard: %d unique traders, %d flagged (>$%dK PNL)",
                     len(self._top_traders), len(self._flagged_wallets), MIN_PNL_USD // 1000)

    async def _fetch_insider_positions(self, client: "httpx.AsyncClient"):
        """Fetch current positions for flagged wallets."""
        self._insider_positions = {}

        # Only check top 20 flagged wallets to stay within rate limits
        sorted_wallets = sorted(
            self._flagged_wallets.values(),
            key=lambda w: w.get("pnl", 0),
            reverse=True
        )[:20]

        for trader in sorted_wallets:
            wallet = trader["wallet"]
            try:
                r = await client.get(f"{DATA_API}/positions", params={
                    "user": wallet,
                    "sizeThreshold": str(MIN_POSITION_SIZE),
                    "limit": "50",
                    "sortBy": "CURRENT",
                    "sortDirection": "DESC",
                })
                if r.status_code == 200:
                    positions = r.json()
                    if not isinstance(positions, list):
                        positions = positions.get("positions", positions.get("data", []))

                    for pos in positions:
                        cid = pos.get("conditionId", "")
                        if not cid:
                            continue

                        if cid not in self._insider_positions:
                            self._insider_positions[cid] = []

                        self._insider_positions[cid].append({
                            "wallet": wallet,
                            "username": trader.get("username", ""),
                            "pnl_rank": trader.get("rank", "?"),
                            "trader_pnl": trader.get("pnl", 0),
                            "suspicious": trader.get("suspicious", False),
                            "asset": pos.get("asset", ""),
                            "outcome": pos.get("outcome", ""),
                            "size": float(pos.get("size", 0) or 0),
                            "avg_price": float(pos.get("avgPrice", 0) or 0),
                            "current_value": float(pos.get("currentValue", 0) or 0),
                            "cash_pnl": float(pos.get("cashPnl", 0) or 0),
                            "pct_pnl": float(pos.get("percentPnl", 0) or 0),
                            "title": pos.get("title", ""),
                        })
            except Exception as e:
                logger.warning("Position fetch failed for %s: %s", wallet[:10], e)
            await asyncio.sleep(0.3)  # Rate limit: 150 req/10s

    def _detect_movements(self):
        """Compare current vs previous positions to detect significant movements.

        Triggers auto-trade when:
        - Multiple insiders enter a market they weren't in before
        - Insider position size increases by >50%
        - Suspicious wallet enters a near-expiry market
        - 3+ wallets converge on same market (new convergence signal)
        """
        if not self._prev_positions:
            return  # First scan, nothing to compare

        new_alerts = []

        for cid, positions in self._insider_positions.items():
            prev = self._prev_positions.get(cid, [])
            prev_wallets = {p["wallet"] for p in prev}
            curr_wallets = {p["wallet"] for p in positions}

            # New insiders entering this market
            new_entrants = curr_wallets - prev_wallets
            if len(new_entrants) >= 2:
                # Multiple insiders entering at once — strong signal
                new_positions = [p for p in positions if p["wallet"] in new_entrants]
                total_new_value = sum(p.get("current_value", 0) for p in new_positions)
                suspicious_new = sum(1 for p in new_positions if p.get("suspicious"))

                alert = {
                    "type": "mass_entry",
                    "condition_id": cid,
                    "title": positions[0].get("title", "") if positions else "",
                    "new_insider_count": len(new_entrants),
                    "total_new_value": round(total_new_value, 2),
                    "suspicious_count": suspicious_new,
                    "direction": self._get_direction(new_positions),
                    "timestamp": time.time(),
                    "auto_triggered": total_new_value > 5000 or suspicious_new > 0,
                }
                new_alerts.append(alert)

                if alert["auto_triggered"]:
                    logger.info("INSIDER ALERT: %d insiders entered %s (%s, $%.0f, %d suspicious)",
                                len(new_entrants), cid[:16], alert["direction"],
                                total_new_value, suspicious_new)

            # Check for significant size increases on existing positions
            for pos in positions:
                wallet = pos["wallet"]
                if wallet not in prev_wallets:
                    continue
                prev_pos = next((p for p in prev if p["wallet"] == wallet), None)
                if not prev_pos:
                    continue
                prev_val = prev_pos.get("current_value", 0)
                curr_val = pos.get("current_value", 0)
                if prev_val > 0 and curr_val > prev_val * 1.5 and (curr_val - prev_val) > 1000:
                    alert = {
                        "type": "size_increase",
                        "condition_id": cid,
                        "title": pos.get("title", ""),
                        "wallet": wallet,
                        "username": pos.get("username", ""),
                        "prev_value": round(prev_val, 2),
                        "new_value": round(curr_val, 2),
                        "increase_pct": round((curr_val - prev_val) / prev_val * 100, 1),
                        "suspicious": pos.get("suspicious", False),
                        "direction": (pos.get("outcome", "") or pos.get("asset", "")).upper(),
                        "timestamp": time.time(),
                        "auto_triggered": pos.get("suspicious", False) or (curr_val - prev_val) > 10000,
                    }
                    new_alerts.append(alert)

        # Whale convergence detection: 3+ wallets entering the same market
        # in this scan window is a very strong directional signal
        for cid, positions in self._insider_positions.items():
            prev = self._prev_positions.get(cid, [])
            prev_wallets = {p["wallet"] for p in prev}
            new_entrants = [p for p in positions if p["wallet"] not in prev_wallets]
            if len(new_entrants) >= CONVERGENCE_THRESHOLD:
                direction = self._get_direction(new_entrants)
                conviction_new = sum(
                    1 for p in new_entrants
                    if self._flagged_wallets.get(p.get("wallet", ""), {}).get("wallet_type") == "conviction"
                )
                total_value = sum(p.get("current_value", 0) for p in new_entrants)
                alert = {
                    "type": "whale_convergence",
                    "condition_id": cid,
                    "title": positions[0].get("title", "") if positions else "",
                    "converging_wallets": len(new_entrants),
                    "conviction_count": conviction_new,
                    "total_value": round(total_value, 2),
                    "direction": direction,
                    "timestamp": time.time(),
                    "auto_triggered": True,  # Convergence always triggers
                }
                new_alerts.append(alert)
                logger.info("WHALE CONVERGENCE: %d wallets (%d conviction) entered %s (%s, $%.0f)",
                            len(new_entrants), conviction_new, cid[:16],
                            direction, total_value)

        self._movement_alerts.extend(new_alerts)
        # Keep only last 100 alerts
        self._movement_alerts = self._movement_alerts[-100:]

        if new_alerts:
            convergence_count = sum(1 for a in new_alerts if a.get("type") == "whale_convergence")
            logger.info("Insider movements: %d alerts (%d auto-triggered, %d convergence)",
                        len(new_alerts), sum(1 for a in new_alerts if a.get("auto_triggered")),
                        convergence_count)

    def _get_direction(self, positions: list[dict]) -> str:
        yes_count = sum(1 for p in positions if "YES" in (p.get("outcome", "") or p.get("asset", "")).upper())
        no_count = len(positions) - yes_count
        if yes_count > no_count:
            return "YES"
        elif no_count > yes_count:
            return "NO"
        return "MIXED"

    def record_resolution(self, condition_id: str, resolved_outcome: str):
        """Record market resolution to track insider accuracy.

        Call this when a market resolves to update per-wallet accuracy scores.
        resolved_outcome: 'YES' or 'NO'
        """
        positions = self._insider_positions.get(condition_id, [])
        if not positions:
            return

        for pos in positions:
            wallet = pos["wallet"]
            outcome = (pos.get("outcome", "") or pos.get("asset", "")).upper()
            was_correct = ("YES" in outcome and resolved_outcome == "YES") or \
                          ("NO" in outcome and resolved_outcome == "NO")

            if wallet not in self._wallet_accuracy:
                self._wallet_accuracy[wallet] = {
                    "wallet": wallet,
                    "username": pos.get("username", ""),
                    "correct": 0,
                    "total": 0,
                    "accuracy": 0,
                    "total_value_correct": 0,
                    "total_value_wrong": 0,
                    "history": [],
                }

            acc = self._wallet_accuracy[wallet]
            acc["total"] += 1
            if was_correct:
                acc["correct"] += 1
                acc["total_value_correct"] += pos.get("current_value", 0)
            else:
                acc["total_value_wrong"] += pos.get("current_value", 0)
            acc["accuracy"] = round(acc["correct"] / acc["total"], 3) if acc["total"] > 0 else 0
            acc["history"].append({
                "condition_id": condition_id,
                "title": pos.get("title", ""),
                "predicted": outcome,
                "actual": resolved_outcome,
                "correct": was_correct,
                "value": pos.get("current_value", 0),
                "timestamp": time.time(),
            })
            # Keep history manageable
            acc["history"] = acc["history"][-50:]

        self._save_cache()
        logger.info("Resolution recorded for %s (%s): %d insiders tracked",
                     condition_id[:16], resolved_outcome, len(positions))

    def get_insider_signal(self, condition_id: str) -> dict:
        """Get insider signal strength for a specific market.

        Returns:
            {
                "has_signal": bool,
                "insider_count": int,          # How many insiders have positions
                "suspicious_count": int,       # How many are flagged suspicious
                "net_direction": "YES"|"NO"|"MIXED",  # Which side insiders favor
                "total_insider_value": float,  # Total $ value of insider positions
                "signal_strength": float,      # 0-1 score (1 = very strong insider signal)
                "insiders": [...]              # Individual insider position details
            }
        """
        positions = self._insider_positions.get(condition_id, [])
        if not positions:
            return {
                "has_signal": False,
                "insider_count": 0,
                "suspicious_count": 0,
                "net_direction": "NONE",
                "total_insider_value": 0,
                "signal_strength": 0,
                "has_convergence": False,
                "convergence_wallets": 0,
                "insiders": [],
            }

        yes_value = 0
        no_value = 0
        conviction_yes = 0
        conviction_no = 0
        suspicious_count = 0
        conviction_count = 0
        market_maker_count = 0
        for p in positions:
            val = abs(p.get("current_value", 0))
            outcome = (p.get("outcome", "") or p.get("asset", "")).upper()
            wallet = p.get("wallet", "")
            trader = self._flagged_wallets.get(wallet, {})
            w_type = trader.get("wallet_type", "unknown")
            weight = trader.get("signal_weight", 1.0)

            if w_type == "market_maker":
                market_maker_count += 1
                continue  # Exclude market makers from directional signal

            if w_type == "conviction":
                conviction_count += 1

            weighted_val = val * weight
            if "YES" in outcome or outcome == "0":
                yes_value += weighted_val
                if w_type == "conviction":
                    conviction_yes += weighted_val
            else:
                no_value += weighted_val
                if w_type == "conviction":
                    conviction_no += weighted_val
            if p.get("suspicious"):
                suspicious_count += 1

        total_value = yes_value + no_value
        if yes_value > no_value * 1.5:
            direction = "YES"
        elif no_value > yes_value * 1.5:
            direction = "NO"
        else:
            direction = "MIXED"

        # Accuracy-weighted signal: insiders with proven track records get more weight
        avg_accuracy = 0
        accuracy_count = 0
        for p in positions:
            if self._flagged_wallets.get(p["wallet"], {}).get("wallet_type") == "market_maker":
                continue  # Don't count market maker accuracy
            acc = self._wallet_accuracy.get(p["wallet"])
            if acc and acc["total"] >= 3:  # Need at least 3 resolved markets
                avg_accuracy += acc["accuracy"]
                accuracy_count += 1
        avg_accuracy = avg_accuracy / accuracy_count if accuracy_count > 0 else 0.5  # Default 50%

        # Signal strength: conviction-weighted
        # Conviction traders drive the signal; market makers are excluded
        non_mm_count = len(positions) - market_maker_count
        count_score = min(non_mm_count / 5, 1.0)
        value_score = min(total_value / 50000, 1.0)
        conviction_score = min(conviction_count / 2, 1.0)  # Cap at 2 conviction traders
        accuracy_score = avg_accuracy

        strength = (count_score * 0.15 + value_score * 0.25 +
                    conviction_score * 0.35 + accuracy_score * 0.25)

        # Whale convergence boost: 3+ wallets on same market = very strong signal
        # Check recent convergence alerts for this condition_id
        has_convergence = False
        convergence_wallets = 0
        for alert in self._movement_alerts[-20:]:  # Check recent alerts
            if (alert.get("type") == "whale_convergence"
                    and alert.get("condition_id") == condition_id
                    and time.time() - alert.get("timestamp", 0) < SCAN_INTERVAL * 2):
                has_convergence = True
                convergence_wallets = alert.get("converging_wallets", 0)
                # Convergence boost: +0.2 per converging wallet above threshold
                convergence_boost = 0.2 * (convergence_wallets - CONVERGENCE_THRESHOLD + 1)
                strength = min(1.0, strength + convergence_boost)
                break

        return {
            "has_signal": non_mm_count > 0,
            "insider_count": non_mm_count,
            "conviction_count": conviction_count,
            "market_maker_count": market_maker_count,
            "suspicious_count": suspicious_count,
            "net_direction": direction,
            "yes_value": round(yes_value, 2),
            "no_value": round(no_value, 2),
            "total_insider_value": round(total_value, 2),
            "signal_strength": round(strength, 3),
            "has_convergence": has_convergence,
            "convergence_wallets": convergence_wallets,
            "insiders": [p for p in positions
                         if self._flagged_wallets.get(p.get("wallet", ""), {}).get("wallet_type") != "market_maker"],
        }

    def get_stats(self) -> dict:
        """Return tracker statistics for the API."""
        # Top accuracy wallets (minimum 3 resolved markets)
        proven_wallets = sorted(
            [a for a in self._wallet_accuracy.values() if a["total"] >= 3],
            key=lambda a: a["accuracy"],
            reverse=True
        )[:10]

        return {
            "running": self._running,
            "tracked_traders": len(self._top_traders),
            "flagged_wallets": len(self._flagged_wallets),
            "markets_with_signals": len(self._insider_positions),
            "wallets_with_accuracy_data": len([a for a in self._wallet_accuracy.values() if a["total"] >= 3]),
            "recent_movement_alerts": len([a for a in self._movement_alerts if time.time() - a.get("timestamp", 0) < 3600]),
            "total_movement_alerts": len(self._movement_alerts),
            "auto_triggered_alerts": len([a for a in self._movement_alerts if a.get("auto_triggered")]),
            "last_scan": self._last_scan,
            "last_scan_ago_min": round((time.time() - self._last_scan) / 60, 1) if self._last_scan else None,
            "top_flagged": sorted(
                self._flagged_wallets.values(),
                key=lambda w: w.get("pnl", 0),
                reverse=True,
            )[:10],
            "top_accurate": [
                {"wallet": w["wallet"][:12] + "...", "username": w["username"],
                 "accuracy": w["accuracy"], "total_resolved": w["total"],
                 "correct": w["correct"]}
                for w in proven_wallets
            ],
            "recent_alerts": self._movement_alerts[-5:],
        }

    def get_market_signals(self, condition_ids: list[str] = None) -> dict:
        """Get insider signals for multiple markets (or all tracked markets)."""
        if condition_ids is None:
            condition_ids = list(self._insider_positions.keys())

        signals = {}
        for cid in condition_ids:
            sig = self.get_insider_signal(cid)
            if sig["has_signal"]:
                signals[cid] = sig
        return signals
