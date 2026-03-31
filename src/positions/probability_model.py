"""Consensus calibration model — aggregates prices across platforms."""
import math
import time
from datetime import date, datetime


def _parse_days_to_expiry(expiry) -> float | None:
    if expiry in (None, "", "ongoing"):
        return None
    text = str(expiry).strip()
    try:
        if "T" in text:
            exp_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return max(0.0, (exp_dt - datetime.now(exp_dt.tzinfo)).total_seconds() / 86400)
        exp_date = date.fromisoformat(text[:10])
        return float(max(0, (exp_date - date.today()).days))
    except (TypeError, ValueError):
        return None


def _horizon_bucket(days_to_expiry: float | None) -> str:
    if days_to_expiry is None:
        return "unknown"
    if days_to_expiry < 1:
        return "intraday"
    if days_to_expiry <= 7:
        return "near"
    if days_to_expiry <= 30:
        return "medium"
    return "long"


class ProbabilityModel:
    """Volume-weighted consensus probability with bounded shrinkage calibration."""

    def __init__(self):
        self._cache: dict[str, dict] = {}
        self._bucket_stats: dict[tuple[str, str, str], dict] = {}

    def update_from_matched_events(self, matched_events: list[dict]):
        """Build consensus from matched events with 2+ platforms."""
        for event in matched_events:
            title = event.get("canonical_title", "")
            if not title:
                continue
            markets = event.get("markets", [])
            if len(markets) < 2:
                continue

            usable_markets = []
            for market in markets:
                yes = market.get("yes_price", 0)
                vol = float(market.get("volume", 0) or 0)
                if 0 < yes < 1:
                    usable_markets.append(
                        {
                            "platform": market.get("platform", ""),
                            "yes_price": yes,
                            "volume": max(vol, 1.0),
                        }
                    )

            if len(usable_markets) < 2:
                continue

            total_vol = sum(m["volume"] for m in usable_markets)
            consensus = sum(m["yes_price"] * m["volume"] for m in usable_markets) / total_vol
            category = event.get("category", "other") or "other"
            days_to_expiry = _parse_days_to_expiry(event.get("expiry"))
            horizon = _horizon_bucket(days_to_expiry)

            deviations = []
            recent_markets = []
            max_deviation = 0.0
            for market in usable_markets:
                deviation = market["yes_price"] - consensus
                abs_deviation = abs(deviation)
                max_deviation = max(max_deviation, abs_deviation)
                if abs_deviation > 0.10:
                    deviations.append({
                        "platform": market["platform"],
                        "price": market["yes_price"],
                        "deviation": round(abs_deviation, 3),
                    })

                recent_markets.append({
                    "platform": market["platform"],
                    "price": round(market["yes_price"], 4),
                    "volume": round(market["volume"], 2),
                    "deviation": round(deviation, 4),
                })
                self._update_bucket_stats(
                    platform=market["platform"],
                    category=category,
                    horizon=horizon,
                    signed_deviation=deviation,
                )

            self._cache[title] = {
                "title": title,
                "category": category,
                "consensus_yes": round(consensus, 4),
                "platform_count": len(usable_markets),
                "max_deviation": round(max_deviation, 4),
                "deviations": deviations,
                "recent_markets": recent_markets,
                "days_to_expiry": days_to_expiry,
                "horizon_bucket": horizon,
                "updated_at": time.time(),
            }

    def _update_bucket_stats(self, platform: str, category: str, horizon: str, signed_deviation: float):
        key = (platform or "unknown", category or "other", horizon or "unknown")
        entry = self._bucket_stats.setdefault(key, {
            "platform": key[0],
            "category": key[1],
            "horizon_bucket": key[2],
            "count": 0,
            "sum_abs_deviation": 0.0,
            "sum_signed_deviation": 0.0,
            "updated_at": 0.0,
        })
        entry["count"] += 1
        entry["sum_abs_deviation"] += abs(signed_deviation)
        entry["sum_signed_deviation"] += signed_deviation
        entry["updated_at"] = time.time()

    def get_consensus(self, title: str) -> dict | None:
        data = self._cache.get(title)
        if data and time.time() - data.get("updated_at", 0) > 86400:
            return None  # Stale (>24h), discard
        return data

    def has_deviation(self, title: str, threshold: float = 0.10) -> bool:
        data = self.get_consensus(title)
        if not data:
            return False
        return data.get("max_deviation", 0) > threshold

    def get_calibration_signal(self, title: str, platform: str, raw_yes: float,
                               category: str = "", days_to_expiry: float | None = None,
                               volume: float = 0) -> dict | None:
        """Shrink a raw market price toward consensus using bucket context."""
        data = self.get_consensus(title)
        if not data or not (0 < raw_yes < 1):
            return None

        horizon = _horizon_bucket(days_to_expiry if days_to_expiry is not None else data.get("days_to_expiry"))
        key = (platform or "unknown", category or data.get("category", "other"), horizon)
        bucket = self._bucket_stats.get(key)
        avg_abs_dev = 0.0
        avg_signed_dev = 0.0
        bucket_count = 0
        if bucket:
            bucket_count = bucket["count"]
            avg_abs_dev = bucket["sum_abs_deviation"] / max(bucket_count, 1)
            avg_signed_dev = bucket["sum_signed_deviation"] / max(bucket_count, 1)

        platform_count = max(2, int(data.get("platform_count", 2) or 2))
        platform_factor = min(1.0, (platform_count - 1) / 4.0)
        volume_factor = min(1.0, math.log10(max(float(volume), 1.0) + 1.0) / 5.0)
        disagreement_factor = min(1.0, avg_abs_dev / 0.20) if avg_abs_dev > 0 else 0.0
        shrink = 0.15 + 0.25 * disagreement_factor + 0.15 * platform_factor + 0.10 * volume_factor
        shrink = max(0.10, min(0.65, shrink))

        consensus_yes = float(data["consensus_yes"])
        adjusted_target = consensus_yes
        if bucket_count >= 3:
            adjusted_target = min(0.99, max(0.01, adjusted_target - avg_signed_dev))

        calibrated_yes = raw_yes + shrink * (adjusted_target - raw_yes)
        calibrated_yes = min(0.99, max(0.01, calibrated_yes))
        shift = calibrated_yes - raw_yes
        preferred_side = "YES" if calibrated_yes >= 0.50 else "NO"
        entry_price = raw_yes if preferred_side == "YES" else (1.0 - raw_yes)
        calibrated_edge_pct = abs(shift) * 100.0 / max(entry_price, 0.01)
        confidence = min(0.99, abs(shift) * (1.0 + shrink))

        return {
            "consensus_yes": round(consensus_yes, 4),
            "calibrated_yes": round(calibrated_yes, 4),
            "shift": round(shift, 4),
            "calibrated_edge_pct": round(calibrated_edge_pct, 2),
            "preferred_side": preferred_side,
            "confidence": round(confidence, 4),
            "bucket": {
                "platform": key[0],
                "category": key[1],
                "horizon_bucket": key[2],
                "count": bucket_count,
                "avg_abs_deviation": round(avg_abs_dev, 4),
                "avg_signed_deviation": round(avg_signed_dev, 4),
            },
        }

    def generate_report(self) -> dict:
        """Summarize current consensus calibration state for the API."""
        now = time.time()
        buckets = []
        for stats in self._bucket_stats.values():
            count = max(stats["count"], 1)
            buckets.append({
                "platform": stats["platform"],
                "category": stats["category"],
                "horizon_bucket": stats["horizon_bucket"],
                "count": stats["count"],
                "avg_abs_deviation": round(stats["sum_abs_deviation"] / count, 4),
                "avg_signed_deviation": round(stats["sum_signed_deviation"] / count, 4),
                "updated_seconds_ago": round(max(0.0, now - stats["updated_at"]), 1),
            })

        buckets.sort(key=lambda item: (-item["avg_abs_deviation"], -item["count"]))

        recent_events = []
        for item in self._cache.values():
            recent_events.append({
                "title": item["title"],
                "category": item["category"],
                "consensus_yes": item["consensus_yes"],
                "max_deviation": item["max_deviation"],
                "platform_count": item["platform_count"],
                "horizon_bucket": item["horizon_bucket"],
                "updated_seconds_ago": round(max(0.0, now - item["updated_at"]), 1),
            })
        recent_events.sort(key=lambda item: (-item["max_deviation"], item["updated_seconds_ago"]))

        return {
            "tracked_events": len(self._cache),
            "tracked_buckets": len(buckets),
            "top_unstable_buckets": buckets[:10],
            "largest_live_disagreements": recent_events[:10],
        }
