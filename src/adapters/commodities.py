"""Synthetic commodity probability adapter built from live futures prices."""
import asyncio
from datetime import date, datetime
import logging
import math
import statistics

try:
    import yfinance as yf
except ImportError:  # pragma: no cover - optional dependency in some test envs
    yf = None

from .base import BaseAdapter
from .models import NormalizedEvent

logger = logging.getLogger("adapters.commodities")


COMMODITY_CONTRACTS = {
    "GC=F": {
        "name": "Gold",
        "slug": "gold",
        "thresholds": [2300, 2500, 2800],
        "default_volatility": 0.16,
    },
    "SI=F": {
        "name": "Silver",
        "slug": "silver",
        "thresholds": [28, 32, 36],
        "default_volatility": 0.26,
    },
    "CL=F": {
        "name": "Crude Oil",
        "slug": "crude-oil",
        "thresholds": [70, 85, 100],
        "default_volatility": 0.35,
    },
    "NG=F": {
        "name": "Natural Gas",
        "slug": "natural-gas",
        "thresholds": [2.5, 3.5, 5.0],
        "default_volatility": 0.55,
    },
    "HG=F": {
        "name": "Copper",
        "slug": "copper",
        "thresholds": [4.0, 4.75, 5.5],
        "default_volatility": 0.24,
    },
    "ZC=F": {
        "name": "Corn",
        "slug": "corn",
        "thresholds": [450, 525, 600],
        "default_volatility": 0.22,
    },
    "ZW=F": {
        "name": "Wheat",
        "slug": "wheat",
        "thresholds": [550, 650, 775],
        "default_volatility": 0.28,
    },
    "ZS=F": {
        "name": "Soybeans",
        "slug": "soybeans",
        "thresholds": [1000, 1150, 1300],
        "default_volatility": 0.21,
    },
}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _probability_exceeds(
    current_price: float,
    threshold: float,
    annual_volatility: float,
    expiry: date,
) -> float:
    """Estimate P(price > threshold at expiry) using a log-normal model."""
    if current_price <= 0 or threshold <= 0:
        return 0.5

    days_remaining = max((expiry - date.today()).days, 1)
    years_remaining = days_remaining / 365.0
    sigma = max(annual_volatility, 0.05) * math.sqrt(years_remaining)
    if sigma <= 0:
        return 1.0 if current_price >= threshold else 0.0

    d2 = (
        math.log(current_price / threshold)
        - 0.5 * (annual_volatility ** 2) * years_remaining
    ) / sigma
    return max(0.001, min(0.999, _norm_cdf(d2)))


def _annualized_volatility(closes: list[float], fallback: float) -> float:
    """Estimate annualized realized volatility from daily closes."""
    if len(closes) < 20:
        return fallback

    returns: list[float] = []
    prev = None
    for close in closes:
        if close is None or close <= 0:
            prev = None
            continue
        if prev is not None:
            returns.append(math.log(close / prev))
        prev = close

    if len(returns) < 10:
        return fallback

    try:
        volatility = statistics.stdev(returns) * math.sqrt(252.0)
    except statistics.StatisticsError:
        return fallback

    return max(0.05, min(volatility, 2.0))


def _rolling_expiries(today: date | None = None) -> list[date]:
    """Return the next two half-year expiries after today."""
    today = today or date.today()
    candidates = [
        date(today.year, 6, 30),
        date(today.year, 12, 31),
        date(today.year + 1, 6, 30),
    ]
    return [expiry for expiry in candidates if expiry > today][:2]


def _format_threshold(value: float) -> str:
    if value >= 100:
        return f"${value:,.0f}"
    if value >= 10:
        return f"${value:,.2f}".rstrip("0").rstrip(".")
    return f"${value:.2f}".rstrip("0").rstrip(".")


class CommoditiesAdapter(BaseAdapter):
    """Build synthetic binary commodity events from live futures prices."""

    PLATFORM_NAME = "commodities"
    RATE_LIMIT_SECONDS = 15.0
    CACHE_TTL_SECONDS = 300.0

    async def _fetch(self) -> list[NormalizedEvent]:
        if yf is None:
            logger.warning("yfinance not installed; commodities adapter disabled")
            return []
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch_sync)

    def _fetch_sync(self) -> list[NormalizedEvent]:
        events: list[NormalizedEvent] = []
        expiries = _rolling_expiries()
        if not expiries:
            return events

        for ticker_symbol, contract in COMMODITY_CONTRACTS.items():
            symbol_events = self._build_events_for_symbol(
                ticker_symbol=ticker_symbol,
                contract=contract,
                expiries=expiries,
            )
            events.extend(symbol_events)

        logger.info("Commodities: generated %d synthetic events", len(events))
        return events

    def _build_events_for_symbol(
        self,
        ticker_symbol: str,
        contract: dict,
        expiries: list[date],
    ) -> list[NormalizedEvent]:
        try:
            ticker = yf.Ticker(ticker_symbol)
            history = ticker.history(period="6mo", interval="1d", auto_adjust=False)
        except Exception as exc:
            logger.warning("Commodity fetch failed for %s: %s", ticker_symbol, exc)
            return []

        if history is None or history.empty or "Close" not in history:
            logger.warning("Commodity history missing for %s", ticker_symbol)
            return []

        closes = [float(value) for value in history["Close"].dropna().tolist()]
        if not closes:
            return []

        current_price = closes[-1]
        annual_volatility = _annualized_volatility(
            closes=closes,
            fallback=float(contract["default_volatility"]),
        )
        volume_series = history["Volume"].dropna() if "Volume" in history else None
        volume = int(float(volume_series.tail(20).mean())) if volume_series is not None and not volume_series.empty else 0

        events: list[NormalizedEvent] = []
        for expiry in expiries:
            for threshold in contract["thresholds"]:
                probability = _probability_exceeds(
                    current_price=current_price,
                    threshold=float(threshold),
                    annual_volatility=annual_volatility,
                    expiry=expiry,
                )
                threshold_label = _format_threshold(float(threshold))
                expiry_label = expiry.isoformat()
                base_event_id = (
                    f"{contract['slug']}-{str(threshold).replace('.', '_')}-{expiry_label}"
                )
                url = f"https://finance.yahoo.com/quote/{ticker_symbol}"

                events.append(
                    NormalizedEvent(
                        platform=self.PLATFORM_NAME,
                        event_id=f"{base_event_id}-above",
                        title=f"Will {contract['name']} exceed {threshold_label} by {expiry_label}?",
                        category="economics",
                        yes_price=round(probability, 4),
                        no_price=round(1.0 - probability, 4),
                        volume=volume,
                        expiry=expiry_label,
                        url=url,
                        spot_price=round(current_price, 4),
                    )
                )
                events.append(
                    NormalizedEvent(
                        platform=self.PLATFORM_NAME,
                        event_id=f"{base_event_id}-below",
                        title=f"Will {contract['name']} fall below {threshold_label} by {expiry_label}?",
                        category="economics",
                        yes_price=round(1.0 - probability, 4),
                        no_price=round(probability, 4),
                        volume=volume,
                        expiry=expiry_label,
                        url=url,
                        spot_price=round(current_price, 4),
                    )
                )

        return events
