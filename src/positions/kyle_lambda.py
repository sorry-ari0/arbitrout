"""Kyle's Lambda Estimator — real-time adverse selection signal from trade flow.

Estimates Kyle's price impact coefficient (λ) using Polymarket CLOB WebSocket
trade data. High λ indicates informed trading; a short-term λ spike relative
to the long-term baseline signals "smart money just arrived."

Uses dual rolling windows (15min short, 2hr long) and produces a directional
multiplier (0.4–1.5) for the auto-trader's scoring pipeline.

Concurrency: single-threaded asyncio. deque operations are atomic in CPython.
No locks needed — the WS receive loop and scoring loop run on the same event loop.
"""
import logging
import time
from collections import defaultdict, deque

logger = logging.getLogger("positions.kyle_lambda")

SHORT_WINDOW_SECONDS = 900
LONG_WINDOW_SECONDS = 7200
MIN_TRADES_SHORT = 10
MIN_TRADES_LONG = 30
MAX_TRADES_PER_MARKET = 5000
SPIKE_THRESHOLD_LOW = 1.5
SPIKE_THRESHOLD_HIGH = 3.0
AGREE_MIN = 1.15
AGREE_MAX = 1.5
OPPOSE_MIN = 0.4
OPPOSE_MAX = 0.8
MIXED_MULTIPLIER = 0.85
FLOW_DIRECTION_THRESHOLD = 0.10


class KyleLambdaEstimator:
    """Estimates Kyle's λ from real-time Polymarket trade flow."""

    def __init__(self):
        self._trades: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=MAX_TRADES_PER_MARKET)
        )
        self._last_price: dict[str, float] = {}

    def on_trade(self, asset_id: str, price: float, size: float,
                 timestamp: float, side: str):
        """Trade callback — registered with PolymarketPriceFeed.on_trade()."""
        self._trades[asset_id].append((timestamp, price, size, side))
        self._last_price[asset_id] = price

    def _prune_old(self, asset_id: str):
        """Remove trades older than LONG_WINDOW_SECONDS."""
        trades = self._trades.get(asset_id)
        if not trades:
            return
        cutoff = time.time() - LONG_WINDOW_SECONDS
        # Support both deque (popleft) and plain list (assigned in tests)
        if isinstance(trades, deque):
            while trades and trades[0][0] < cutoff:
                trades.popleft()
        else:
            while trades and trades[0][0] < cutoff:
                trades.pop(0)

    def _compute_lambda(self, asset_id: str, window_seconds: int) -> "tuple[float, int] | None":
        """Compute λ via OLS: λ = Σ(Q_t · Δp_t) / Σ(Q_t²). Returns (λ, n) or None."""
        self._prune_old(asset_id)
        trades = self._trades.get(asset_id)
        if not trades:
            return None

        cutoff = time.time() - window_seconds
        window_trades = [(ts, p, s, side) for ts, p, s, side in trades if ts >= cutoff]

        if len(window_trades) < MIN_TRADES_SHORT:
            return None

        sum_qd = 0.0
        sum_qq = 0.0

        for i in range(1, len(window_trades)):
            _, prev_price, _, _ = window_trades[i - 1]
            ts, cur_price, size, side = window_trades[i]
            delta_p = cur_price - prev_price

            if side in ("buy", "BUY"):
                signed_vol = size
            elif side in ("sell", "SELL"):
                signed_vol = -size
            else:
                if delta_p > 0:
                    signed_vol = size
                elif delta_p < 0:
                    signed_vol = -size
                else:
                    signed_vol = size

            sum_qd += signed_vol * delta_p
            sum_qq += signed_vol * signed_vol

        if sum_qq == 0:
            return None

        lambda_val = sum_qd / sum_qq
        return (lambda_val, len(window_trades))

    def _get_flow_direction(self, asset_id: str, window_seconds: int) -> str:
        """Net flow direction: 'YES' (buying), 'NO' (selling), or 'MIXED'."""
        trades = self._trades.get(asset_id)
        if not trades:
            return "MIXED"

        cutoff = time.time() - window_seconds
        buy_vol = 0.0
        sell_vol = 0.0
        prev_price = None

        for ts, price, size, side in trades:
            if ts < cutoff:
                prev_price = price
                continue
            if side in ("buy", "BUY"):
                buy_vol += size
            elif side in ("sell", "SELL"):
                sell_vol += size
            else:
                if prev_price is not None:
                    if price > prev_price:
                        buy_vol += size
                    elif price < prev_price:
                        sell_vol += size
                    else:
                        buy_vol += size
                else:
                    buy_vol += size
            prev_price = price

        total = buy_vol + sell_vol
        if total == 0:
            return "MIXED"
        net = buy_vol - sell_vol
        if abs(net) / total < FLOW_DIRECTION_THRESHOLD:
            return "MIXED"
        return "YES" if net > 0 else "NO"

    def get_lambda_signal(self, market_id: str, our_direction: str) -> dict:
        """Get λ-based directional multiplier for scoring."""
        asset_id = self._resolve_asset_id(market_id)

        neutral = {
            "multiplier": 1.0, "short_lambda": None, "long_lambda": None,
            "lambda_ratio": None, "flow_direction": "MIXED",
            "agrees_with_arb": None, "n_trades_short": 0,
            "n_trades_long": 0, "sufficient_data": False,
        }

        if asset_id is None:
            return neutral

        short_result = self._compute_lambda(asset_id, SHORT_WINDOW_SECONDS)
        long_result = self._compute_lambda(asset_id, LONG_WINDOW_SECONDS)

        if short_result is None or long_result is None:
            n_short = short_result[1] if short_result else 0
            n_long = long_result[1] if long_result else 0
            neutral["n_trades_short"] = n_short
            neutral["n_trades_long"] = n_long
            return neutral

        short_lambda, n_short = short_result
        long_lambda, n_long = long_result

        if long_lambda <= 0:
            neutral["short_lambda"] = short_lambda
            neutral["long_lambda"] = long_lambda
            neutral["n_trades_short"] = n_short
            neutral["n_trades_long"] = n_long
            neutral["sufficient_data"] = True
            return neutral

        lambda_ratio = short_lambda / long_lambda

        if lambda_ratio <= SPIKE_THRESHOLD_LOW:
            return {
                "multiplier": 1.0, "short_lambda": short_lambda,
                "long_lambda": long_lambda, "lambda_ratio": lambda_ratio,
                "flow_direction": self._get_flow_direction(asset_id, SHORT_WINDOW_SECONDS),
                "agrees_with_arb": None, "n_trades_short": n_short,
                "n_trades_long": n_long, "sufficient_data": True,
            }

        flow_dir = self._get_flow_direction(asset_id, SHORT_WINDOW_SECONDS)
        t = min((lambda_ratio - SPIKE_THRESHOLD_LOW) /
                (SPIKE_THRESHOLD_HIGH - SPIKE_THRESHOLD_LOW), 1.0)

        if flow_dir == our_direction:
            agrees = True
            multiplier = AGREE_MIN + t * (AGREE_MAX - AGREE_MIN)
        elif flow_dir == "MIXED":
            agrees = None
            multiplier = MIXED_MULTIPLIER
        else:
            agrees = False
            multiplier = OPPOSE_MAX - t * (OPPOSE_MAX - OPPOSE_MIN)

        logger.info(
            "Kyle λ signal: market=%s ratio=%.2f flow=%s arb=%s mult=%.2f (n=%d/%d)",
            market_id[:20], lambda_ratio, flow_dir, our_direction,
            multiplier, n_short, n_long,
        )

        return {
            "multiplier": round(multiplier, 3), "short_lambda": round(short_lambda, 8),
            "long_lambda": round(long_lambda, 8), "lambda_ratio": round(lambda_ratio, 3),
            "flow_direction": flow_dir, "agrees_with_arb": agrees,
            "n_trades_short": n_short, "n_trades_long": n_long,
            "sufficient_data": True,
        }

    def _resolve_asset_id(self, market_id: str) -> "str | None":
        """Resolve market_id to buffered asset_id (exact or prefix match)."""
        if market_id in self._trades and self._trades[market_id]:
            return market_id
        for aid in self._trades:
            if aid.startswith(market_id):
                return aid
        return None

    def get_stats(self) -> dict:
        """Return estimator status for diagnostics."""
        total_trades = sum(len(d) for d in self._trades.values())
        return {"tracked_markets": len(self._trades), "total_buffered_trades": total_trades}
