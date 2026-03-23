"""Insider/whale tracker — monitors top Polymarket traders and their positions.

Uses Polymarket's public Data API to:
1. Fetch top traders from the leaderboard (by PNL) across multiple categories
2. Track their current positions on specific markets (tiered polling)
3. Detect suspicious patterns (high win rates, pre-resolution trades)
4. Detect insider exits (conviction traders leaving markets)
5. Scan whale-sized trades and market holders for additional signals
6. Provide signals to the auto trader for position scoring

Data API: https://data-api.polymarket.com (no auth required)
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

logger = logging.getLogger("positions.insider_tracker")

DATA_API = "https://data-api.polymarket.com"

# Thresholds
MIN_PNL_USD = 5000           # Track traders with >$5K lifetime PNL (lowered to catch mid-size edge traders)
MIN_WIN_RATE = 0.65          # Flag traders with >65% win rate
SUSPICIOUS_WIN_RATE = 0.80   # Highly suspicious above 80%
MIN_POSITION_SIZE = 100      # Minimum $100 position to count as signal
LEADERBOARD_SIZE = 50        # Top 50 traders to monitor
SCAN_INTERVAL = 900          # 15 minutes between full scans
CACHE_TTL = 600              # Cache insider data for 10 minutes
CONVERGENCE_THRESHOLD = 3    # 3+ wallets entering same market = convergence signal

# Leaderboard categories — expanded to cover politics/economics specialists
LEADERBOARD_CATEGORIES = ["OVERALL", "CRYPTO", "POLITICS", "ECONOMICS", "FINANCE"]
LEADERBOARD_TIME_PERIODS = ["ALL", "MONTH"]

# Wallet classification thresholds
# Conviction = consistently winning at high rates, regardless of volume
ROI_CONVICTION_MIN = 0.15    # >15% ROI = conviction trader (consistent winners)
ROI_MARKET_MAKER_MAX = 0.05  # <5% ROI = market maker (spread capture, noise)
VOL_MARKET_MAKER_MIN = 100_000_000  # >$100M volume + low ROI = market maker

# Statistical edge detection — identifies traders profiting beyond chance
# A trader with >12% ROI at $50K+ volume has ~95% probability of real edge
# (random trading at fair prices converges to 0% ROI by law of large numbers)
EDGE_ROI_MIN = 0.12          # >12% ROI signals likely information advantage
EDGE_VOLUME_MIN = 50_000     # Minimum $50K volume to distinguish from luck
EDGE_HIGH_ROI = 0.25         # >25% ROI at any volume = extremely strong signal

# Auto-promotion thresholds
AUTO_PROMOTE_PNL = 500_000      # $500K+ PNL (lowered to catch mid-size edge traders)
AUTO_PROMOTE_ROI = 0.18         # 18%+ ROI
AUTO_PROMOTE_SCANS = 2          # Must appear on 2+ consecutive scans
AUTO_DEMOTE_SCANS = 10          # Drop off leaderboard for 10+ scans → demote

# Whale trade scanning
WHALE_TRADE_MIN_USD = 5000      # $5K+ trades count as whale activity
WHALE_TRADE_WINDOW = 3600       # 1-hour rolling window for whale trade tracking

# High-conviction watchlist: known profitable directional traders
# These get 5x signal weight and bypass normal ROI thresholds
# Source: Polymarket leaderboard analysis (2026-03-20)
HIGH_CONVICTION_WATCHLIST = {
    "0x56687bf447db6ffa42ffe2204a05edaa20f55839": "Theo4",        # $22M PNL, 51% ROI
    "0x1f2dd6d473f3e824cd2f8a89d9c69fb96f6ad0cf": "Fredi9999",    # $16.6M PNL, 22% ROI
    "0x863134d00841b2e200492805a01e1e2f5defaa53": "RepTrump",     # $7.5M PNL, 54% ROI
    "0x78b9ac44a6d7d7a076c14e0ad518b301b63c6b76": "Len9311238",   # $8.7M PNL, 53% ROI
    "0x885783a5e42d297c3532081ebf5c14ba0e9b0a44": "BetTom42",     # $5.6M PNL, 50% ROI
    "0x23786fdad0073692157c6d7dc81f281843a35fcb": "mikatrade77",  # $5.2M PNL, 47% ROI
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
        self._scan_count: int = 0  # For tiered polling rotation

        # Auto-promotion tracking: wallet -> consecutive scan count
        self._consecutive_scans: dict[str, int] = {}

        # Whale trade cache: condition_id -> [{size, side, timestamp}]
        self._whale_trades: dict[str, list[dict]] = {}

        # Market holders cache: condition_id -> {holders: [...], fetched_at: float}
        self._holders_cache: dict[str, dict] = {}

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
                    merged = dict(HIGH_CONVICTION_WATCHLIST)
                    merged.update(data["wallets"])
                    self._conviction_watchlist = merged
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
                self._consecutive_scans = data.get("consecutive_scans", {})
                self._whale_trades = data.get("whale_trades", {})
                logger.info("Loaded %d tracked insiders from cache", len(self._flagged_wallets))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load insider cache: %s", e)

    def _save_cache(self):
        """Persist insider data to disk."""
        path = self.data_dir / "insider_signals.json"
        tmp = str(path) + ".tmp"
        data = {
            "top_traders": self._top_traders,
            "insider_positions": self._insider_positions,
            "prev_positions": self._prev_positions,
            "flagged_wallets": self._flagged_wallets,
            "wallet_accuracy": self._wallet_accuracy,
            "movement_alerts": self._movement_alerts[-100:],  # Keep last 100 alerts
            "last_scan": self._last_scan,
            "consecutive_scans": self._consecutive_scans,
            "whale_trades": self._whale_trades,
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

    # ================================================================
    # MAIN SCAN
    # ================================================================

    async def scan(self):
        """Full scan cycle: fetch leaderboard, get positions, detect movements, flag insiders."""
        if not httpx:
            logger.warning("httpx not available for insider tracking")
            return

        # Save previous positions for movement detection
        self._prev_positions = dict(self._insider_positions)
        self._scan_count += 1

        async with httpx.AsyncClient(timeout=20.0) as client:
            # Step 1: Fetch top traders by PNL (expanded categories)
            await self._fetch_leaderboard(client)

            # Step 2: Auto-promote/demote watchlist based on leaderboard data
            self._auto_refresh_watchlist()

            # Step 3: Tiered position polling for flagged wallets
            await self._fetch_insider_positions(client)

            # Step 4: Scan whale-sized trades across all markets
            await self._scan_whale_trades(client)

        # Step 5: Detect movements — entries, exits, size changes, convergence
        self._detect_movements()

        # Prune stale whale trades (older than 1 hour)
        self._prune_whale_trades()

        self._last_scan = time.time()
        self._save_cache()
        logger.info("Insider scan #%d: %d tracked, %d flagged, %d markets, %d alerts, %d whale trades",
                     self._scan_count, len(self._top_traders), len(self._flagged_wallets),
                     len(self._insider_positions), len(self._movement_alerts),
                     sum(len(v) for v in self._whale_trades.values()))

    # ================================================================
    # TASK 1: LEADERBOARD (expanded categories + auto-refresh)
    # ================================================================

    async def _fetch_leaderboard(self, client: "httpx.AsyncClient"):
        """Fetch top traders from Polymarket leaderboard across multiple categories."""
        traders = []
        for category in LEADERBOARD_CATEGORIES:
            for time_period in LEADERBOARD_TIME_PERIODS:
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

        self._top_traders = unique_traders[:LEADERBOARD_SIZE * 3]  # Keep top 150 (more categories)

        # Track which wallets appeared this scan for auto-promotion
        current_wallets = set()

        # Pre-compute lowercase sets for O(1) lookups (avoid rebuilding per iteration)
        watchlist_lower = {k.lower() for k in self._conviction_watchlist}
        mm_lower = {k.lower() for k in KNOWN_MARKET_MAKERS}

        # Flag wallets with high PNL or suspicious patterns
        # Classify: conviction > edge_trader > market_maker > unknown
        # "edge_trader" = mid-size accounts with statistically improbable returns,
        # likely profiting from information advantage (not just big whales)
        for t in self._top_traders:
            wallet = t.get("proxyWallet", "")
            pnl = float(t.get("pnl", 0) or 0)
            volume = float(t.get("vol", 0) or 0)

            if pnl < MIN_PNL_USD:
                continue

            current_wallets.add(wallet)

            # ROI = PNL/volume. At scale, random trading converges to ~0% ROI.
            # High ROI over significant volume = statistical evidence of real edge.
            roi = pnl / volume if volume > 0 else 0
            is_suspicious = roi > EDGE_ROI_MIN

            # Classify wallet type
            # Priority: watchlist > known MM > high-ROI conviction > edge trader > unknown
            if wallet.lower() in watchlist_lower:
                wallet_type = "conviction"
                signal_weight = 5.0  # Watchlist wallets get 5x weight
            elif wallet.lower() in mm_lower:
                wallet_type = "market_maker"
                signal_weight = 0.0  # Market makers excluded from directional signals
            elif roi >= ROI_CONVICTION_MIN:
                # High ROI = proven conviction trader (big or small account)
                wallet_type = "conviction"
                signal_weight = 3.0 + min(roi * 5, 2.0)  # 3-5x weight, scales with ROI
            elif roi >= EDGE_HIGH_ROI:
                # Very high ROI at any volume = extremely strong statistical edge
                wallet_type = "edge_trader"
                signal_weight = 3.5  # Strong signal even at smaller account size
            elif roi >= EDGE_ROI_MIN and volume >= EDGE_VOLUME_MIN:
                # Moderate ROI over meaningful volume = likely information advantage
                # A 12%+ ROI over $50K+ volume is statistically unlikely by chance alone
                wallet_type = "edge_trader"
                signal_weight = 2.0 + min(roi * 8, 2.0)  # 2-4x weight
            elif roi <= ROI_MARKET_MAKER_MAX and volume >= VOL_MARKET_MAKER_MIN:
                wallet_type = "market_maker"
                signal_weight = 0.0
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

        # Update consecutive scan counts for auto-promotion tracking
        for wallet in current_wallets:
            self._consecutive_scans[wallet] = self._consecutive_scans.get(wallet, 0) + 1
        # Wallets that disappeared: reset count
        for wallet in list(self._consecutive_scans.keys()):
            if wallet not in current_wallets:
                self._consecutive_scans[wallet] = self._consecutive_scans.get(wallet, 0) - 1
                if self._consecutive_scans[wallet] <= -AUTO_DEMOTE_SCANS:
                    del self._consecutive_scans[wallet]

        logger.info("Leaderboard: %d unique traders from %d categories, %d flagged (>$%dK PNL)",
                     len(self._top_traders), len(LEADERBOARD_CATEGORIES),
                     len(self._flagged_wallets), MIN_PNL_USD // 1000)

    def _auto_refresh_watchlist(self):
        """Auto-promote wallets that meet criteria; auto-demote stale ones."""
        promoted = []
        demoted = []

        for wallet, info in self._flagged_wallets.items():
            wallet_lower = wallet.lower()
            # Skip if already in hardcoded sets
            if wallet_lower in {k.lower() for k in HIGH_CONVICTION_WATCHLIST}:
                continue
            if wallet_lower in {k.lower() for k in KNOWN_MARKET_MAKERS}:
                continue

            pnl = info.get("pnl", 0)
            roi = info.get("roi_pct", 0) / 100.0
            consecutive = self._consecutive_scans.get(wallet, 0)

            # Auto-promote: high PNL + high ROI + consistent presence
            if (pnl >= AUTO_PROMOTE_PNL and roi >= AUTO_PROMOTE_ROI
                    and consecutive >= AUTO_PROMOTE_SCANS
                    and wallet not in self._conviction_watchlist):
                self._conviction_watchlist[wallet] = info.get("username", wallet[:12])
                info["wallet_type"] = "conviction"
                info["signal_weight"] = 4.0  # Below manual 5.0
                promoted.append(info.get("username", wallet[:12]))

        # Auto-demote: wallets that fell off leaderboard for too long
        for wallet in list(self._conviction_watchlist.keys()):
            if wallet.lower() in {k.lower() for k in HIGH_CONVICTION_WATCHLIST}:
                continue  # Never demote hardcoded
            consecutive = self._consecutive_scans.get(wallet, 0)
            if consecutive <= -AUTO_DEMOTE_SCANS:
                name = self._conviction_watchlist.pop(wallet, wallet[:12])
                demoted.append(name)

        if promoted or demoted:
            if promoted:
                logger.info("Watchlist auto-promoted: %s", ", ".join(promoted))
            if demoted:
                logger.info("Watchlist auto-demoted: %s", ", ".join(demoted))
            # Persist updated watchlist
            self.update_watchlist(self._conviction_watchlist)

    # ================================================================
    # TASK 5: TIERED POSITION POLLING
    # ================================================================

    async def _fetch_insider_positions(self, client: "httpx.AsyncClient"):
        """Fetch current positions for flagged wallets using tiered polling.

        Tier 1 (every scan): conviction watchlist + top accuracy wallets (10)
        Tier 2 (every 2nd scan): high-PNL wallets (15)
        Tier 3 (every 4th scan): remaining flagged wallets (25)

        Only clears positions for wallets actually polled this scan to avoid
        false exit alerts for wallets that were simply not queried this cycle.
        """
        polled_wallets: set[str] = set()  # Track which wallets we actually query

        # Remove old positions for wallets we'll poll (they'll be re-fetched).
        # Keep positions for wallets NOT polled this cycle (they're still valid).
        # This is done after we know which wallets to poll — see below.

        # Build tiered wallet list sorted by signal quality, not raw PNL
        def _wallet_priority(w):
            acc = self._wallet_accuracy.get(w.get("wallet", ""), {})
            accuracy_bonus = acc.get("accuracy", 0.5) if acc.get("total", 0) >= 3 else 0.5
            return w.get("signal_weight", 1.0) * accuracy_bonus

        all_wallets = sorted(self._flagged_wallets.values(),
                             key=_wallet_priority, reverse=True)

        # Tier 1: conviction + edge_traders + top accuracy (always polled)
        tier1 = [w for w in all_wallets if w.get("wallet_type") in ("conviction", "edge_trader")][:10]
        tier1_addrs = {w["wallet"] for w in tier1}

        # Tier 2: next best by priority (every 2nd scan)
        tier2 = [w for w in all_wallets
                 if w["wallet"] not in tier1_addrs][:15]
        tier2_addrs = {w["wallet"] for w in tier2}

        # Tier 3: remaining (every 4th scan)
        tier3 = [w for w in all_wallets
                 if w["wallet"] not in tier1_addrs and w["wallet"] not in tier2_addrs][:25]

        # Decide which tiers to poll this scan
        wallets_to_poll = list(tier1)  # Always poll tier 1
        if self._scan_count % 2 == 0:
            wallets_to_poll.extend(tier2)
        if self._scan_count % 4 == 0:
            wallets_to_poll.extend(tier3)

        # Clear old positions ONLY for wallets we're about to poll
        wallets_to_clear = {w["wallet"] for w in wallets_to_poll}
        for cid in list(self._insider_positions.keys()):
            self._insider_positions[cid] = [
                p for p in self._insider_positions[cid] if p["wallet"] not in wallets_to_clear
            ]
            if not self._insider_positions[cid]:
                del self._insider_positions[cid]

        for trader in wallets_to_poll:
            wallet = trader["wallet"]
            polled_wallets.add(wallet)
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

        logger.info("Position poll: %d wallets (T1=%d T2=%d T3=%d), %d markets",
                     len(wallets_to_poll), len(tier1),
                     len(tier2) if self._scan_count % 2 == 0 else 0,
                     len(tier3) if self._scan_count % 4 == 0 else 0,
                     len(self._insider_positions))

    # ================================================================
    # TASK 6: WHALE TRADE SCANNING + MARKET HOLDERS
    # ================================================================

    async def _scan_whale_trades(self, client: "httpx.AsyncClient"):
        """Scan for whale-sized trades ($5K+) across all Polymarket markets."""
        try:
            r = await client.get(f"{DATA_API}/trades", params={
                "filterType": "CASH",
                "filterAmount": str(WHALE_TRADE_MIN_USD),
                "limit": "100",
                "takerOnly": "true",
            })
            if r.status_code != 200:
                return

            trades = r.json()
            if not isinstance(trades, list):
                trades = trades.get("trades", trades.get("data", []))

            now = time.time()
            new_count = 0
            for t in trades:
                cid = t.get("conditionId", "")
                if not cid:
                    continue
                ts = t.get("timestamp", 0)
                # Convert string timestamps if needed
                if isinstance(ts, str):
                    try:
                        ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        ts = now
                # Only track recent trades (within window)
                if now - ts > WHALE_TRADE_WINDOW:
                    continue

                if cid not in self._whale_trades:
                    self._whale_trades[cid] = []

                # Deduplicate by trade hash if available
                tx_hash = t.get("transactionHash", "")
                existing_hashes = {wt.get("tx_hash", "") for wt in self._whale_trades[cid]}
                if tx_hash and tx_hash in existing_hashes:
                    continue

                side = t.get("side", "").upper()
                outcome = (t.get("outcome", "") or "").upper()
                # Determine direction: BUY YES vs BUY NO vs SELL YES etc.
                if "YES" in outcome:
                    direction = "YES" if side == "BUY" else "NO"
                elif "NO" in outcome:
                    direction = "NO" if side == "BUY" else "YES"
                else:
                    direction = side

                size = float(t.get("size", 0) or 0)
                price = float(t.get("price", 0) or 0)
                usd_value = size * price if price > 0 else size

                self._whale_trades[cid].append({
                    "size": round(usd_value, 2),
                    "direction": direction,
                    "timestamp": ts,
                    "wallet": t.get("proxyWallet", ""),
                    "tx_hash": tx_hash,
                    "title": t.get("title", ""),
                })
                new_count += 1

            if new_count > 0:
                logger.info("Whale trade scan: %d new trades >$%d across %d markets",
                            new_count, WHALE_TRADE_MIN_USD,
                            len(self._whale_trades))

        except Exception as e:
            logger.warning("Whale trade scan failed: %s", e)

    async def fetch_market_holders(self, client: "httpx.AsyncClient",
                                   condition_id: str) -> list[dict]:
        """Fetch top 20 holders for a specific market. Cached for 10 min."""
        cached = self._holders_cache.get(condition_id)
        if cached and time.time() - cached.get("fetched_at", 0) < CACHE_TTL:
            return cached.get("holders", [])

        try:
            r = await client.get(f"{DATA_API}/holders", params={
                "market": condition_id,
                "limit": "20",
            })
            if r.status_code == 200:
                data = r.json()
                holders = []
                # Response is array of {token, holders: [...]}
                if isinstance(data, list):
                    for token_group in data:
                        for h in token_group.get("holders", []):
                            holders.append({
                                "wallet": h.get("proxyWallet", ""),
                                "amount": float(h.get("amount", 0) or 0),
                                "outcome_index": h.get("outcomeIndex", 0),
                                "name": h.get("name", h.get("pseudonym", "")),
                            })
                self._holders_cache[condition_id] = {
                    "holders": holders,
                    "fetched_at": time.time(),
                }
                return holders
        except Exception as e:
            logger.warning("Holders fetch failed for %s: %s", condition_id[:12], e)

        return []

    def _prune_whale_trades(self):
        """Remove whale trades older than the tracking window."""
        cutoff = time.time() - WHALE_TRADE_WINDOW
        for cid in list(self._whale_trades.keys()):
            self._whale_trades[cid] = [
                t for t in self._whale_trades[cid] if t.get("timestamp", 0) > cutoff
            ]
            if not self._whale_trades[cid]:
                del self._whale_trades[cid]

    # ================================================================
    # TASK 4: MOVEMENT DETECTION (entries + exits + size changes)
    # ================================================================

    def _detect_movements(self):
        """Compare current vs previous positions to detect significant movements.

        Detects:
        - Mass entry: 2+ insiders entering a market they weren't in before
        - Size increase: existing position grows >50% and >$1K
        - Whale convergence: 3+ wallets entering same market in same scan
        - Insider exit: conviction traders leaving a market (NEW)
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
        for cid, positions in self._insider_positions.items():
            prev = self._prev_positions.get(cid, [])
            prev_wallets = {p["wallet"] for p in prev}
            new_entrants = [p for p in positions if p["wallet"] not in prev_wallets]
            if len(new_entrants) >= CONVERGENCE_THRESHOLD:
                direction = self._get_direction(new_entrants)
                conviction_new = sum(
                    1 for p in new_entrants
                    if self._flagged_wallets.get(p.get("wallet", ""), {}).get("wallet_type") in ("conviction", "edge_trader")
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
                    "auto_triggered": True,
                }
                new_alerts.append(alert)
                logger.info("WHALE CONVERGENCE: %d wallets (%d conviction) entered %s (%s, $%.0f)",
                            len(new_entrants), conviction_new, cid[:16],
                            direction, total_value)

        # TASK 4: Insider EXIT detection — conviction traders leaving a market
        for cid, prev_positions in self._prev_positions.items():
            curr = self._insider_positions.get(cid, [])
            curr_wallets = {p["wallet"] for p in curr}
            prev_wallets = {p["wallet"] for p in prev_positions}

            exited_wallets = prev_wallets - curr_wallets
            for wallet in exited_wallets:
                trader = self._flagged_wallets.get(wallet, {})
                if trader.get("wallet_type") not in ("conviction", "edge_trader"):
                    continue  # Only alert on conviction/edge trader exits

                prev_pos = next((p for p in prev_positions if p["wallet"] == wallet), None)
                if not prev_pos:
                    continue

                prev_value = prev_pos.get("current_value", 0)
                if prev_value < 500:
                    continue  # Skip tiny positions

                alert = {
                    "type": "insider_exit",
                    "condition_id": cid,
                    "title": prev_pos.get("title", ""),
                    "wallet": wallet,
                    "username": trader.get("username", prev_pos.get("username", "")),
                    "prev_value": round(prev_value, 2),
                    "direction": (prev_pos.get("outcome", "") or prev_pos.get("asset", "")).upper(),
                    "wallet_type": trader.get("wallet_type", "conviction"),
                    "signal_weight": trader.get("signal_weight", 5.0),
                    "timestamp": time.time(),
                    "auto_triggered": False,  # Exits are informational, not auto-traded
                }
                new_alerts.append(alert)
                logger.info("INSIDER EXIT: %s exited %s (%s, was $%.0f)",
                            trader.get("username", wallet[:12]), cid[:16],
                            alert["direction"], prev_value)

        self._movement_alerts.extend(new_alerts)
        self._movement_alerts = self._movement_alerts[-100:]

        if new_alerts:
            exit_count = sum(1 for a in new_alerts if a.get("type") == "insider_exit")
            convergence_count = sum(1 for a in new_alerts if a.get("type") == "whale_convergence")
            logger.info("Insider movements: %d alerts (%d auto-triggered, %d convergence, %d exits)",
                        len(new_alerts), sum(1 for a in new_alerts if a.get("auto_triggered")),
                        convergence_count, exit_count)

    def _get_direction(self, positions: list[dict]) -> str:
        yes_count = sum(1 for p in positions if "YES" in (p.get("outcome", "") or p.get("asset", "")).upper())
        no_count = len(positions) - yes_count
        if yes_count > no_count:
            return "YES"
        elif no_count > yes_count:
            return "NO"
        return "MIXED"

    # ================================================================
    # ACCURACY TRACKING
    # ================================================================

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
            acc["history"] = acc["history"][-50:]

        self._save_cache()
        logger.info("Resolution recorded for %s (%s): %d insiders tracked",
                     condition_id[:16], resolved_outcome, len(positions))

    # ================================================================
    # SIGNAL GENERATION (TASK 7: position-relative sizing)
    # ================================================================

    def get_insider_signal(self, condition_id: str, market_volume: float = 0) -> dict:
        """Get insider signal strength for a specific market.

        Args:
            condition_id: The Polymarket condition ID
            market_volume: Optional market volume for position-relative sizing boost

        Returns:
            {
                "has_signal": bool,
                "insider_count": int,
                "suspicious_count": int,
                "net_direction": "YES"|"NO"|"MIXED",
                "total_insider_value": float,
                "signal_strength": float,      # 0-1 score
                "has_convergence": bool,
                "convergence_wallets": int,
                "whale_trade_count": int,       # NEW: whale trades in last hour
                "whale_trade_direction": str,   # NEW: net direction of whale trades
                "has_insider_exits": bool,       # NEW: conviction traders exited recently
                "insiders": [...]
            }
        """
        positions = self._insider_positions.get(condition_id, [])
        whale_trades = self._whale_trades.get(condition_id, [])

        if not positions and not whale_trades:
            return {
                "has_signal": False,
                "insider_count": 0,
                "suspicious_count": 0,
                "net_direction": "NONE",
                "total_insider_value": 0,
                "signal_strength": 0,
                "has_convergence": False,
                "convergence_wallets": 0,
                "whale_trade_count": 0,
                "whale_trade_direction": "NONE",
                "has_insider_exits": False,
                "insiders": [],
            }

        yes_value = 0
        no_value = 0
        raw_total_value = 0  # Unweighted, for concentration ratio
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
                continue

            if w_type in ("conviction", "edge_trader"):
                conviction_count += 1

            raw_total_value += val
            weighted_val = val * weight
            if "YES" in outcome or outcome == "0":
                yes_value += weighted_val
            else:
                no_value += weighted_val
            if p.get("suspicious"):
                suspicious_count += 1

        total_value = yes_value + no_value
        if yes_value > no_value * 1.5:
            direction = "YES"
        elif no_value > yes_value * 1.5:
            direction = "NO"
        else:
            direction = "MIXED"

        # Accuracy-weighted signal
        avg_accuracy = 0
        accuracy_count = 0
        for p in positions:
            if self._flagged_wallets.get(p["wallet"], {}).get("wallet_type") == "market_maker":
                continue
            acc = self._wallet_accuracy.get(p["wallet"])
            if acc and acc["total"] >= 3:
                avg_accuracy += acc["accuracy"]
                accuracy_count += 1
        avg_accuracy = avg_accuracy / accuracy_count if accuracy_count > 0 else 0.5

        # Signal strength: conviction-weighted
        non_mm_count = len(positions) - market_maker_count
        count_score = min(non_mm_count / 5, 1.0)
        value_score = min(total_value / 50000, 1.0)
        conviction_score = min(conviction_count / 2, 1.0)
        accuracy_score = avg_accuracy

        strength = (count_score * 0.15 + value_score * 0.25 +
                    conviction_score * 0.35 + accuracy_score * 0.25)

        # Whale convergence boost
        has_convergence = False
        convergence_wallets = 0
        for alert in self._movement_alerts[-20:]:
            if (alert.get("type") == "whale_convergence"
                    and alert.get("condition_id") == condition_id
                    and time.time() - alert.get("timestamp", 0) < SCAN_INTERVAL * 2):
                has_convergence = True
                convergence_wallets = alert.get("converging_wallets", 0)
                convergence_boost = 0.2 * (convergence_wallets - CONVERGENCE_THRESHOLD + 1)
                strength = min(1.0, strength + convergence_boost)
                break

        # TASK 6: Whale trade signal boost
        whale_trade_count = len(whale_trades)
        whale_yes = sum(1 for t in whale_trades if t.get("direction") == "YES")
        whale_no = sum(1 for t in whale_trades if t.get("direction") == "NO")
        if whale_yes > whale_no * 1.5:
            whale_direction = "YES"
        elif whale_no > whale_yes * 1.5:
            whale_direction = "NO"
        elif whale_trade_count > 0:
            whale_direction = "MIXED"
        else:
            whale_direction = "NONE"

        # Boost signal if whale trades align with insider direction
        if whale_trade_count >= 3 and whale_direction == direction and direction != "MIXED":
            whale_boost = min(0.15, whale_trade_count * 0.03)
            strength = min(1.0, strength + whale_boost)

        # TASK 7: Position-relative sizing boost (uses raw value, not weight-inflated)
        if market_volume > 0 and raw_total_value > 0:
            concentration = raw_total_value / market_volume
            if concentration > 0.10:
                strength = min(1.0, strength + 0.25)
            elif concentration > 0.05:
                strength = min(1.0, strength + 0.15)

        # TASK 4: Check for recent insider exits (bearish signal)
        has_insider_exits = False
        for alert in self._movement_alerts[-20:]:
            if (alert.get("type") == "insider_exit"
                    and alert.get("condition_id") == condition_id
                    and time.time() - alert.get("timestamp", 0) < SCAN_INTERVAL * 2):
                has_insider_exits = True
                break

        return {
            "has_signal": non_mm_count > 0 or whale_trade_count >= 3,
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
            "whale_trade_count": whale_trade_count,
            "whale_trade_direction": whale_direction,
            "has_insider_exits": has_insider_exits,
            "insiders": [p for p in positions
                         if self._flagged_wallets.get(p.get("wallet", ""), {}).get("wallet_type") != "market_maker"],
        }

    # ================================================================
    # EXIT SIGNAL (for exit engine integration)
    # ================================================================

    def get_exit_signals(self, condition_id: str) -> list[dict]:
        """Return recent exit alerts for a market (conviction traders leaving).

        Used by the exit engine to detect when smart money exits our positions.
        """
        recent_exits = []
        cutoff = time.time() - SCAN_INTERVAL * 2  # Last 2 scan windows
        for alert in self._movement_alerts:
            if (alert.get("type") == "insider_exit"
                    and alert.get("condition_id") == condition_id
                    and alert.get("timestamp", 0) > cutoff):
                recent_exits.append(alert)
        return recent_exits

    # ================================================================
    # STATS & UTILITIES
    # ================================================================

    def get_stats(self) -> dict:
        """Return tracker statistics for the API."""
        proven_wallets = sorted(
            [a for a in self._wallet_accuracy.values() if a["total"] >= 3],
            key=lambda a: a["accuracy"],
            reverse=True
        )[:10]

        return {
            "running": self._running,
            "scan_count": self._scan_count,
            "tracked_traders": len(self._top_traders),
            "flagged_wallets": len(self._flagged_wallets),
            "conviction_watchlist_size": len(self._conviction_watchlist),
            "markets_with_signals": len(self._insider_positions),
            "markets_with_whale_trades": len(self._whale_trades),
            "total_whale_trades": sum(len(v) for v in self._whale_trades.values()),
            "wallets_with_accuracy_data": len([a for a in self._wallet_accuracy.values() if a["total"] >= 3]),
            "recent_movement_alerts": len([a for a in self._movement_alerts if time.time() - a.get("timestamp", 0) < 3600]),
            "total_movement_alerts": len(self._movement_alerts),
            "auto_triggered_alerts": len([a for a in self._movement_alerts if a.get("auto_triggered")]),
            "exit_alerts": len([a for a in self._movement_alerts if a.get("type") == "insider_exit"]),
            "last_scan": self._last_scan,
            "last_scan_ago_min": round((time.time() - self._last_scan) / 60, 1) if self._last_scan else None,
            "leaderboard_categories": LEADERBOARD_CATEGORIES,
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
