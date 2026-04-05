"""Weather market scanner — finds edge in Kalshi daily weather markets using NWS forecasts.

Kalshi lists daily temperature and precipitation markets for major US cities.
NWS (National Weather Service) provides free, high-quality forecasts via API.
Edge: NWS forecasts are more accurate than market pricing, especially 1-3 days out.

Strategy:
  1. Fetch Kalshi weather markets (temperature brackets, precipitation yes/no)
  2. Fetch NWS point forecast for each city
  3. Optionally merge NASA EONET open events near the city/date (storms, floods, etc.)
  4. Compare forecast to market brackets
  5. If forecast strongly favors one bracket, generate opportunity
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta

try:
    import httpx
except ImportError:
    httpx = None

from positions.eonet_client import (
    eonet_enabled,
    fetch_open_events,
    filter_relevant_events,
    precip_adjustment_bp,
)

logger = logging.getLogger("positions.weather_scanner")

# NWS forecast endpoints (free, no API key needed)
NWS_API_BASE = "https://api.weather.gov"

# City → NWS grid point mapping (lat/lon for NWS forecast lookup)
CITY_COORDINATES = {
    "new york": {"lat": 40.7128, "lon": -74.0060, "aliases": ["nyc", "new york city"]},
    "chicago": {"lat": 41.8781, "lon": -87.6298, "aliases": ["chi"]},
    "los angeles": {"lat": 34.0522, "lon": -118.2437, "aliases": ["la", "los angeles"]},
    "miami": {"lat": 25.7617, "lon": -80.1918, "aliases": ["mia"]},
    "dallas": {"lat": 32.7767, "lon": -96.7970, "aliases": ["dfw"]},
    "denver": {"lat": 39.7392, "lon": -104.9903, "aliases": ["den"]},
    "seattle": {"lat": 47.6062, "lon": -122.3321, "aliases": ["sea"]},
    "phoenix": {"lat": 33.4484, "lon": -112.0740, "aliases": ["phx"]},
    "atlanta": {"lat": 33.749, "lon": -84.388, "aliases": ["atl"]},
    "boston": {"lat": 42.3601, "lon": -71.0589, "aliases": ["bos"]},
}

# Cache NWS grid point lookups (lat/lon → grid endpoint)
_grid_cache: dict[str, dict] = {}
_grid_cache_time: dict[str, float] = {}
_GRID_CACHE_TTL = 86400  # 24h — grid points don't change


class WeatherScanner:
    """Scans Kalshi weather markets and finds mispriced brackets using NWS data."""

    def __init__(self, kalshi_adapter=None, decision_logger=None):
        self._kalshi = kalshi_adapter
        self._dlog = decision_logger
        self._cache: list[dict] = []
        self._cache_time: float = 0
        self._cache_ttl: float = 600.0  # 10 min cache
        self._headers = {"User-Agent": "Arbitrout/1.0 (weather-scanner)"}

    async def scan(self) -> list[dict]:
        """Scan for weather market opportunities. Returns list of opportunity dicts."""
        if time.time() - self._cache_time < self._cache_ttl:
            return self._cache

        if httpx is None:
            logger.warning("httpx not available for weather scanner")
            return []

        opportunities = []
        try:
            async with httpx.AsyncClient(timeout=15, headers=self._headers) as client:
                eonet_events: list[dict] = []
                if eonet_enabled():
                    eonet_events = await fetch_open_events(client, days=14)
                    if eonet_events:
                        logger.info("Weather scanner: loaded %d open EONET events", len(eonet_events))

                # Fetch Kalshi weather markets
                weather_markets = await self._fetch_weather_markets(client)
                if not weather_markets:
                    logger.info("Weather scanner: no weather markets found")
                    return []

                # Group by city and date
                grouped = self._group_markets(weather_markets)

                # For each city+date, fetch NWS forecast and compare
                for key, markets in grouped.items():
                    city, target_date = key
                    forecast = await self._get_nws_forecast(client, city, target_date)
                    if not forecast:
                        continue

                    forecast = self._merge_eonet_forecast(forecast, city, target_date, eonet_events)

                    opp = self._evaluate_opportunity(city, target_date, markets, forecast)
                    if opp:
                        opportunities.append(opp)

        except Exception as e:
            logger.warning("Weather scanner error: %s", e)

        logger.info("Weather scanner: %d opportunities from %d markets",
                     len(opportunities), sum(len(m) for m in grouped.values()) if 'grouped' in dir() else 0)
        self._cache = opportunities
        self._cache_time = time.time()
        return opportunities

    async def _fetch_weather_markets(self, client: "httpx.AsyncClient") -> list[dict]:
        """Fetch weather-related markets from Kalshi public API."""
        markets = []
        try:
            resp = await client.get(
                "https://api.elections.kalshi.com/trade-api/v2/events",
                params={"status": "open", "series_ticker": "KXHIGHTEMP", "limit": 50}
            )
            if resp.status_code == 200:
                data = resp.json()
                events = data.get("events", [])
                for event in events:
                    event_markets = event.get("markets", [])
                    for m in event_markets:
                        m["_event_title"] = event.get("title", "")
                        m["_category"] = "temperature"
                    markets.extend(event_markets)
        except Exception as e:
            logger.debug("Kalshi temp markets fetch failed: %s", e)

        # Also try precipitation
        try:
            resp = await client.get(
                "https://api.elections.kalshi.com/trade-api/v2/events",
                params={"status": "open", "series_ticker": "KXRAINY", "limit": 50}
            )
            if resp.status_code == 200:
                data = resp.json()
                events = data.get("events", [])
                for event in events:
                    event_markets = event.get("markets", [])
                    for m in event_markets:
                        m["_event_title"] = event.get("title", "")
                        m["_category"] = "precipitation"
                    markets.extend(event_markets)
        except Exception as e:
            logger.debug("Kalshi precip markets fetch failed: %s", e)

        logger.info("Weather scanner: fetched %d weather markets from Kalshi", len(markets))
        return markets

    def _group_markets(self, markets: list[dict]) -> dict[tuple, list[dict]]:
        """Group markets by (city, date)."""
        grouped: dict[tuple, list[dict]] = {}
        for m in markets:
            title = (m.get("title", "") or m.get("_event_title", "")).lower()
            city = self._extract_city(title)
            target_date = self._extract_date(m)
            if city and target_date:
                key = (city, target_date)
                grouped.setdefault(key, []).append(m)
        return grouped

    def _extract_city(self, title: str) -> str | None:
        """Extract city name from market title."""
        title_lower = title.lower()
        for city, info in CITY_COORDINATES.items():
            if city in title_lower:
                return city
            for alias in info.get("aliases", []):
                if alias in title_lower:
                    return city
        return None

    def _extract_date(self, market: dict) -> str | None:
        """Extract target date from market metadata."""
        # Try expiration_time or close_time
        for field in ["expiration_time", "close_time", "expected_expiration_time"]:
            val = market.get(field, "")
            if val:
                try:
                    dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                    return dt.strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    pass
        return None

    async def _get_nws_forecast(self, client: "httpx.AsyncClient", city: str, target_date: str) -> dict | None:
        """Get NWS forecast for a city on a specific date.

        Returns dict with 'high_temp', 'low_temp', 'precip_chance' or None.
        """
        coords = CITY_COORDINATES.get(city)
        if not coords:
            return None

        cache_key = f"{coords['lat']},{coords['lon']}"

        # Step 1: Get grid point (cached for 24h)
        grid = _grid_cache.get(cache_key)
        if not grid or time.time() - _grid_cache_time.get(cache_key, 0) > _GRID_CACHE_TTL:
            try:
                resp = await client.get(
                    f"{NWS_API_BASE}/points/{coords['lat']},{coords['lon']}"
                )
                if resp.status_code != 200:
                    logger.debug("NWS grid lookup failed for %s: %d", city, resp.status_code)
                    return None
                data = resp.json()
                grid = {
                    "forecast_url": data.get("properties", {}).get("forecast", ""),
                    "forecast_hourly_url": data.get("properties", {}).get("forecastHourly", ""),
                }
                _grid_cache[cache_key] = grid
                _grid_cache_time[cache_key] = time.time()
            except Exception as e:
                logger.debug("NWS grid lookup error for %s: %s", city, e)
                return None

        if not grid.get("forecast_url"):
            return None

        # Step 2: Get daily forecast
        try:
            resp = await client.get(grid["forecast_url"])
            if resp.status_code != 200:
                return None
            data = resp.json()
            periods = data.get("properties", {}).get("periods", [])

            # Find the period matching target_date
            for period in periods:
                start = period.get("startTime", "")
                try:
                    period_date = datetime.fromisoformat(start.replace("Z", "+00:00")).strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    continue

                if period_date == target_date:
                    temp = period.get("temperature")
                    is_daytime = period.get("isDaytime", True)
                    precip_pct = 0
                    precip_detail = period.get("probabilityOfPrecipitation", {})
                    if isinstance(precip_detail, dict):
                        precip_pct = precip_detail.get("value", 0) or 0

                    forecast = {}
                    if is_daytime:
                        forecast["high_temp"] = temp
                    else:
                        forecast["low_temp"] = temp
                    forecast["precip_chance"] = precip_pct
                    forecast["short_forecast"] = period.get("shortForecast", "")

                    # Try to get both high and low from adjacent periods
                    for p2 in periods:
                        try:
                            p2_date = datetime.fromisoformat(
                                p2.get("startTime", "").replace("Z", "+00:00")
                            ).strftime("%Y-%m-%d")
                        except (ValueError, TypeError):
                            continue
                        if p2_date == target_date and p2 is not period:
                            if p2.get("isDaytime", True) and "high_temp" not in forecast:
                                forecast["high_temp"] = p2.get("temperature")
                            elif not p2.get("isDaytime", True) and "low_temp" not in forecast:
                                forecast["low_temp"] = p2.get("temperature")
                            p2_precip = p2.get("probabilityOfPrecipitation", {})
                            if isinstance(p2_precip, dict):
                                forecast["precip_chance"] = max(
                                    forecast["precip_chance"],
                                    p2_precip.get("value", 0) or 0
                                )

                    if "high_temp" in forecast or "low_temp" in forecast:
                        return forecast
        except Exception as e:
            logger.debug("NWS forecast fetch error for %s: %s", city, e)

        return None

    def _merge_eonet_forecast(
        self,
        forecast: dict,
        city: str,
        target_date: str,
        eonet_events: list[dict],
    ) -> dict:
        """Blend NASA EONET hazard context into NWS precip (see https://eonet.gsfc.nasa.gov/)."""
        if not eonet_events or not eonet_enabled():
            out = dict(forecast)
            out.setdefault("eonet", {"active": False})
            return out

        coords = CITY_COORDINATES.get(city)
        if not coords:
            return dict(forecast)

        relevant = filter_relevant_events(
            eonet_events, coords["lon"], coords["lat"], target_date
        )
        if not relevant:
            out = dict(forecast)
            out["eonet"] = {"active": False, "nearby": 0}
            return out

        delta_bp, summary = precip_adjustment_bp(relevant)
        out = dict(forecast)
        pc = float(out.get("precip_chance") or 0)
        new_pc = max(0.0, min(95.0, pc + delta_bp))
        out["precip_chance"] = new_pc
        out["eonet"] = {
            "active": True,
            "precip_adjust_bp": delta_bp,
            "precip_nws": round(pc, 1),
            "events": summary[:10],
        }
        if delta_bp != 0:
            logger.info(
                "EONET adjust %s %s: precip %.1f%% → %.1f%% (%d nearby events, %+d bp)",
                city,
                target_date,
                pc,
                new_pc,
                len(summary),
                delta_bp,
            )
        return out

    def _evaluate_opportunity(self, city: str, target_date: str,
                              markets: list[dict], forecast: dict) -> dict | None:
        """Compare NWS forecast to market brackets and generate opportunity if mispriced.

        Returns opportunity dict or None.
        """
        high_temp = forecast.get("high_temp")
        precip_chance = forecast.get("precip_chance", 0)

        best_edge = None
        best_market = None
        best_side = None

        for m in markets:
            category = m.get("_category", "temperature")
            title = m.get("title", m.get("_event_title", "")).lower()

            if category == "temperature" and high_temp is not None:
                # Parse bracket from title: "above X°F" or "between X and Y"
                bracket = self._parse_temp_bracket(title)
                if not bracket:
                    continue

                # Determine if forecast says YES or NO
                if bracket["type"] == "above":
                    forecast_yes_prob = self._temp_above_probability(high_temp, bracket["threshold"])
                elif bracket["type"] == "below":
                    forecast_yes_prob = self._temp_below_probability(high_temp, bracket["threshold"])
                elif bracket["type"] == "between":
                    forecast_yes_prob = self._temp_between_probability(
                        high_temp, bracket["low"], bracket["high"])
                else:
                    continue

                # Get market price
                yes_price = float(m.get("yes_bid", 0) or m.get("last_price", 0) or 0)
                if yes_price <= 0:
                    continue

                # Edge = forecast probability - market probability
                edge = forecast_yes_prob - yes_price
                if abs(edge) > 0.10:  # Need 10%+ edge to trade
                    if abs(edge) > (abs(best_edge) if best_edge else 0):
                        best_edge = edge
                        best_market = m
                        best_side = "YES" if edge > 0 else "NO"

            elif category == "precipitation":
                if precip_chance <= 0:
                    continue
                # Precip markets are typically "Will it rain in X?"
                forecast_yes_prob = precip_chance / 100.0
                yes_price = float(m.get("yes_bid", 0) or m.get("last_price", 0) or 0)
                if yes_price <= 0:
                    continue

                edge = forecast_yes_prob - yes_price
                if abs(edge) > 0.10:
                    if abs(edge) > (abs(best_edge) if best_edge else 0):
                        best_edge = edge
                        best_market = m
                        best_side = "YES" if edge > 0 else "NO"

        if not best_market or best_edge is None:
            return None

        yes_price = float(best_market.get("yes_bid", 0) or best_market.get("last_price", 0) or 0)
        no_price = 1.0 - yes_price
        entry_price = yes_price if best_side == "YES" else no_price
        profit_pct = round(abs(best_edge) / entry_price * 100, 2) if entry_price > 0 else 0

        ticker = best_market.get("ticker", "")
        event_id = best_market.get("event_ticker", ticker)

        opp = {
            "opportunity_type": "weather_forecast",
            "title": f"Weather: {city.title()} {target_date} — {best_side}",
            "canonical_title": f"Weather {city} {target_date}",
            "platform": "kalshi",
            "city": city,
            "target_date": target_date,
            "forecast": forecast,
            "market_ticker": ticker,
            "side": best_side,
            "edge": round(abs(best_edge), 4),
            "forecast_prob": round(
                (forecast.get("high_temp", 0) if best_market.get("_category") == "temperature"
                 else precip_chance / 100.0), 4),
            "market_price": round(yes_price, 4),
            "profit_pct": profit_pct,
            "buy_yes_platform": "kalshi",
            "buy_no_platform": "kalshi",
            "buy_yes_price": round(yes_price, 4),
            "buy_no_price": round(no_price, 4),
            "buy_yes_market_id": event_id,
            "buy_no_market_id": event_id,
            "expiry": target_date,
            "volume": int(float(best_market.get("volume", 0) or 0)),
        }
        if forecast.get("eonet"):
            opp["eonet"] = forecast["eonet"]

        logger.info("Weather opportunity: %s %s | edge=%.1f%% | side=%s | price=%.2f",
                     city.title(), target_date, abs(best_edge) * 100, best_side, entry_price)

        if self._dlog:
            self._dlog.log_opportunity_detected(
                title=opp["title"],
                strategy_type="weather_forecast",
                spread_pct=profit_pct,
                platforms=["kalshi"],
                yes_price=yes_price,
                no_price=no_price,
                is_synthetic=False,
                volume=opp["volume"],
                event_ids=[event_id],
            )

        return opp

    def _parse_temp_bracket(self, title: str) -> dict | None:
        """Parse temperature bracket from market title."""
        import re
        title = title.lower()

        # "above 60°f" / "over 60" / ">= 60"
        m = re.search(r'(?:above|over|>=?|at least)\s*(\d+)', title)
        if m:
            return {"type": "above", "threshold": int(m.group(1))}

        # "below 40°f" / "under 40" / "<= 40"
        m = re.search(r'(?:below|under|<=?|at most)\s*(\d+)', title)
        if m:
            return {"type": "below", "threshold": int(m.group(1))}

        # "between 50 and 60" / "50-60"
        m = re.search(r'(?:between\s*)?(\d+)\s*(?:and|-|to)\s*(\d+)', title)
        if m:
            return {"type": "between", "low": int(m.group(1)), "high": int(m.group(2))}

        return None

    def _temp_above_probability(self, forecast_temp: float, threshold: int) -> float:
        """Estimate probability temp will be above threshold given forecast.

        Uses a simple normal distribution approximation with ±5°F std dev
        for NWS forecasts (empirical accuracy for 1-3 day forecasts).
        """
        diff = forecast_temp - threshold
        # Sigmoid approximation: P(above) ≈ 1 / (1 + exp(-diff/3))
        import math
        try:
            return 1.0 / (1.0 + math.exp(-diff / 3.0))
        except OverflowError:
            return 0.0 if diff < 0 else 1.0

    def _temp_below_probability(self, forecast_temp: float, threshold: int) -> float:
        return 1.0 - self._temp_above_probability(forecast_temp, threshold)

    def _temp_between_probability(self, forecast_temp: float, low: int, high: int) -> float:
        p_above_low = self._temp_above_probability(forecast_temp, low)
        p_above_high = self._temp_above_probability(forecast_temp, high)
        return max(0.0, p_above_low - p_above_high)
