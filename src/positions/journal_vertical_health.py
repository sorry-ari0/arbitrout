"""Live production guard: pause non-news strategy verticals that lost money in recent paper journal.

Reads trade_journal_paper.json — only entries with mode=paper and news_sleeve=false contribute.
News-driven closes are excluded so a losing scanner pure_prediction sleeve does not block News: trades.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from positions.wallet_config import is_paper_mode

logger = logging.getLogger("positions.journal_vertical_health")

_CACHE: frozenset[str] | None = None
_CACHE_AT: float = 0.0


def _journal_paper_path() -> Path:
    custom = os.environ.get("TRADE_JOURNAL_PAPER_PATH", "").strip()
    if custom:
        return Path(custom)
    return Path(__file__).resolve().parent.parent / "data" / "positions" / "trade_journal_paper.json"


def _health_disabled() -> bool:
    return os.environ.get("JOURNAL_HEALTH_DISABLE", "").lower() in ("1", "true", "yes")


def _lookback_seconds() -> float:
    try:
        days = float(os.environ.get("JOURNAL_HEALTH_LOOKBACK_DAYS", "2"))
    except ValueError:
        days = 2.0
    return max(0.25, days) * 86400.0


def _min_closes_to_pause() -> int:
    try:
        n = int(os.environ.get("JOURNAL_HEALTH_MIN_CLOSES", "1"))
    except ValueError:
        n = 1
    return max(1, n)


def _cache_ttl_sec() -> float:
    try:
        return max(5.0, float(os.environ.get("JOURNAL_HEALTH_CACHE_SEC", "60")))
    except ValueError:
        return 60.0


def entry_news_sleeve(entry: dict) -> bool:
    """True if this journal close was news-driven (never used to pause the news vertical)."""
    if entry.get("news_sleeve") is True:
        return True
    if entry.get("strategy_type") == "news_driven":
        return True
    name = entry.get("name") or ""
    if isinstance(name, str) and name.startswith("News:"):
        return True
    return False


def entry_is_paper(entry: dict) -> bool:
    return (entry.get("mode") or "paper") == "paper"


def package_is_news_sleeve(pkg: dict) -> bool:
    return bool(pkg.get("_news_driven") or pkg.get("strategy_type") == "news_driven")


def resolve_opportunity_vertical_strategy(opp: dict) -> str:
    """strategy_type that would be set on a package if this (non-news) opportunity executed."""
    ot = opp.get("opportunity_type") or ""
    if ot == "multi_outcome_arb":
        return "multi_outcome_arb"
    if ot == "portfolio_no":
        return "portfolio_no"
    if ot == "weather_forecast":
        return "weather_forecast"
    if ot == "political_synthetic":
        return "political_synthetic"
    if ot == "crypto_synthetic":
        return "crypto_synthetic"

    buy_yes_platform = opp.get("buy_yes_platform") or ""
    buy_no_platform = opp.get("buy_no_platform") or ""
    yes_mid = opp.get("buy_yes_market_id") or ""
    no_mid = opp.get("buy_no_market_id") or ""
    is_cross_platform = buy_yes_platform != buy_no_platform and bool(yes_mid and no_mid)
    if is_cross_platform:
        return "cross_platform_arb"
    if opp.get("is_synthetic"):
        return "synthetic_derivative"
    return "pure_prediction"


def _compute_paused_verticals() -> frozenset[str]:
    path = _journal_paper_path()
    if not path.exists():
        return frozenset()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("journal_vertical_health: cannot read %s: %s", path, e)
        return frozenset()

    entries = data.get("entries") or []
    cutoff = time.time() - _lookback_seconds()
    min_n = _min_closes_to_pause()

    # strategy_type -> (sum_pnl, count)
    agg: dict[str, list[float]] = {}

    for e in entries:
        if not isinstance(e, dict):
            continue
        closed_at = e.get("closed_at")
        if closed_at is None:
            continue
        try:
            ts = float(closed_at)
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            continue
        if not entry_is_paper(e):
            continue
        if entry_news_sleeve(e):
            continue

        st = e.get("strategy_type") or "unknown"
        pnl = float(e.get("pnl", 0) or 0)
        if st not in agg:
            agg[st] = [0.0, 0.0]
        agg[st][0] += pnl
        agg[st][1] += 1.0

    paused: set[str] = set()
    for st, (total_pnl, n) in agg.items():
        if n < min_n:
            continue
        if total_pnl < 0:
            paused.add(st)

    if paused:
        logger.info(
            "journal_vertical_health: pausing non-news live verticals (paper journal last %.2fd): %s",
            _lookback_seconds() / 86400.0,
            sorted(paused),
        )
    return frozenset(paused)


def get_paused_non_news_verticals() -> frozenset[str]:
    """Cached set of strategy_type values to block for live non-news opens."""
    global _CACHE, _CACHE_AT
    if _health_disabled():
        return frozenset()
    now = time.time()
    if _CACHE is not None and (now - _CACHE_AT) < _cache_ttl_sec():
        return _CACHE
    _CACHE = _compute_paused_verticals()
    _CACHE_AT = now
    return _CACHE


def invalidate_paused_verticals_cache() -> None:
    global _CACHE, _CACHE_AT
    _CACHE = None
    _CACHE_AT = 0.0


def live_journal_allows_package_open(pkg: dict) -> tuple[bool, str]:
    """Live only: block if package strategy vertical is on pause list; news sleeve always allowed."""
    if _health_disabled() or is_paper_mode():
        return True, ""
    if package_is_news_sleeve(pkg):
        return True, ""
    st = pkg.get("strategy_type") or "unknown"
    paused = get_paused_non_news_verticals()
    if st in paused:
        lb = _lookback_seconds() / 86400.0
        return False, (
            f"Live open blocked: strategy vertical {st!r} has negative aggregate PnL in recent "
            f"paper journal (non-news closes only, last {lb:.2f} days). "
            "News-driven trades are unaffected. Set JOURNAL_HEALTH_DISABLE=true to skip this check."
        )
    return True, ""


def live_non_news_opportunity_should_pause(opp: dict) -> tuple[bool, str]:
    """For auto-trader: True if this opportunity's vertical is paused (live only, not news-tagged)."""
    if _health_disabled() or is_paper_mode():
        return False, ""
    if opp.get("_news_driven"):
        return False, ""
    key = resolve_opportunity_vertical_strategy(opp)
    if key in get_paused_non_news_verticals():
        return True, key
    return False, ""


def journal_health_status() -> dict:
    """Safe diagnostics for APIs."""
    return {
        "disabled": _health_disabled(),
        "lookback_days": round(_lookback_seconds() / 86400.0, 4),
        "min_closes": _min_closes_to_pause(),
        "journal_path": str(_journal_paper_path()),
        "paused_non_news_verticals": sorted(get_paused_non_news_verticals()),
    }
