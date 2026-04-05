"""Unit tests for NASA EONET helper (weather scanner integration)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from positions.eonet_client import (
    event_relevant_to_city_date,
    haversine_km,
    precip_adjustment_bp,
)


def test_haversine_chicago_to_miami_rough():
    # Chicago
    lon1, lat1 = -87.6298, 41.8781
    # Miami
    lon2, lat2 = -80.1918, 25.7617
    d = haversine_km(lon1, lat1, lon2, lat2)
    assert 1700 < d < 2100


def test_event_relevant_distance_and_date():
    ev = {
        "id": "TEST1",
        "title": "Test storm",
        "categories": [{"id": "severeStorms", "title": "Severe Storms"}],
        "geometry": [
            {
                "type": "Point",
                "coordinates": [-87.6298, 41.8781],
                "date": "2026-06-15T12:00:00Z",
            }
        ],
    }
    # Same day as geometry
    assert event_relevant_to_city_date(ev, -87.6298, 41.8781, "2026-06-15", max_km=50)
    # Too far
    assert not event_relevant_to_city_date(ev, -80.0, 25.0, "2026-06-15", max_km=50)


def test_precip_adjustment_storm_and_fire():
    events = [
        {
            "id": "A",
            "title": "Cyclone",
            "categories": [{"id": "severeStorms"}],
            "geometry": [],
        },
        {
            "id": "B",
            "title": "Fire",
            "categories": [{"id": "wildfires"}],
            "geometry": [],
        },
    ]
    delta, summ = precip_adjustment_bp(events)
    assert delta == 7  # +10 and -3, capped logic: pos=10 neg=-3
    assert len(summ) == 2
