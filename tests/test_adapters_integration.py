import asyncio

from adapters.base import BaseAdapter
from adapters.models import NormalizedEvent
from adapters.registry import AdapterRegistry


class _FakeAdapter(BaseAdapter):
    PLATFORM_NAME = "fake"
    RATE_LIMIT_SECONDS = 1

    def __init__(self, events=None):
        super().__init__()
        self._events = events or []

    async def _fetch(self) -> list[NormalizedEvent]:
        return list(self._events)


def _make_event(event_id: str) -> NormalizedEvent:
    return NormalizedEvent(
        platform="fake",
        event_id=event_id,
        title=f"Event {event_id}",
        category="test",
        yes_price=0.45,
        no_price=0.55,
        volume=100,
        expiry="2026-12-31",
        url=f"https://example.com/{event_id}",
    )


def test_register_adapter_tracks_status():
    registry = AdapterRegistry()
    adapter = _FakeAdapter([_make_event("e1")])

    registry.register_adapter(adapter)

    status = next(item for item in registry.get_all_status() if item["name"] == "fake")
    assert status["status"] == "offline"
    assert status["event_count"] == 0


def test_fetch_all_includes_registered_adapter_events():
    registry = AdapterRegistry()
    registry._adapters = {}
    registry._status = {}

    expected = [_make_event("e1"), _make_event("e2")]
    registry.register_adapter(_FakeAdapter(expected))

    events = asyncio.run(registry.fetch_all())

    assert [event.event_id for event in events] == ["e1", "e2"]
    status = next(item for item in registry.get_all_status() if item["name"] == "fake")
    assert status["status"] == "online"
    assert status["event_count"] == 2
