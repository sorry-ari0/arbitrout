"""NASA EONET (Earth Observatory Natural Event Tracker) API v3 — natural hazard context.

Used to enrich NWS-based weather market signals with nearby open events (storms, floods,
wildfires, etc.). API is public, no key: https://eonet.gsfc.nasa.gov/
"""
from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime
from typing import Any

logger = logging.getLogger("positions.eonet")

EONET_EVENTS_URL = "https://eonet.gsfc.nasa.gov/api/v3/events"

# Extra precip probability points (NWS %) when EONET shows a hazard near the city/date window.
# Conservative — EONET is contextual, not a replacement for NWS.
_CATEGORY_PRECIP_BP: dict[str, int] = {
    "severeStorms": 10,
    "floods": 12,
    "landslides": 8,
    "dustHaze": 4,
    "snow": 6,
    "volcanoes": 2,
    "waterColor": 0,
    "seaLakeIce": 0,
    "wildfires": -3,
    "drought": -6,
    "tempExtremes": 3,
    "earthquakes": 0,
    "manmade": 0,
}

_DEFAULT_MAX_KM = 450.0
_DEFAULT_DAY_WINDOW = 2


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in kilometers."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _parse_geometry_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _event_category_ids(event: dict) -> set[str]:
    out: set[str] = set()
    for c in event.get("categories") or []:
        if isinstance(c, dict) and c.get("id"):
            out.add(str(c["id"]))
    return out


def event_relevant_to_city_date(
    event: dict,
    city_lon: float,
    city_lat: float,
    target_date: str,
    *,
    max_km: float = _DEFAULT_MAX_KM,
    day_window: int = _DEFAULT_DAY_WINDOW,
) -> bool:
    """True if any Point geometry is within max_km and date within ±day_window of target_date."""
    try:
        t0 = date.fromisoformat(target_date)
    except ValueError:
        return False

    for g in event.get("geometry") or []:
        if not isinstance(g, dict) or g.get("type") != "Point":
            continue
        coords = g.get("coordinates")
        if not coords or len(coords) < 2:
            continue
        try:
            lon, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue
        if haversine_km(city_lon, city_lat, lon, lat) > max_km:
            continue
        gd = _parse_geometry_date(g.get("date"))
        if gd is None:
            continue
        if abs((gd - t0).days) <= day_window:
            return True
    return False


def precip_adjustment_bp(relevant_events: list[dict]) -> tuple[int, list[dict]]:
    """Return (delta_precip_percentage_points, summary rows for logging)."""
    if not relevant_events:
        return 0, []

    # Use max positive and max negative category deltas; cap combined swing.
    pos = 0
    neg = 0
    summary: list[dict] = []
    seen: set[str] = set()

    for ev in relevant_events:
        eid = str(ev.get("id", ""))
        title = (ev.get("title") or "")[:80]
        for cat in _event_category_ids(ev):
            if cat not in _CATEGORY_PRECIP_BP:
                continue
            bp = _CATEGORY_PRECIP_BP[cat]
            if bp > 0:
                pos = max(pos, bp)
            elif bp < 0:
                neg = min(neg, bp)
        if eid and eid not in seen:
            seen.add(eid)
            summary.append(
                {
                    "id": eid,
                    "title": title,
                    "categories": sorted(_event_category_ids(ev)),
                }
            )

    delta = pos + neg
    delta = max(-15, min(18, delta))
    return delta, summary


async def fetch_open_events(client: Any, *, days: int = 14) -> list[dict]:
    """Fetch open natural events from EONET v3 (status=open, recent days window)."""
    try:
        resp = await client.get(
            EONET_EVENTS_URL,
            params={"days": int(days), "status": "open"},
            timeout=20.0,
        )
        if resp.status_code != 200:
            logger.debug("EONET fetch failed: HTTP %s", resp.status_code)
            return []
        data = resp.json()
        return list(data.get("events") or [])
    except Exception as e:
        logger.warning("EONET fetch error: %s", e)
        return []


def filter_relevant_events(
    events: list[dict],
    city_lon: float,
    city_lat: float,
    target_date: str,
) -> list[dict]:
    return [
        ev
        for ev in events
        if isinstance(ev, dict)
        and event_relevant_to_city_date(ev, city_lon, city_lat, target_date)
    ]


def eonet_enabled() -> bool:
    return os.environ.get("EONET_WEATHER_ENABLED", "true").lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
