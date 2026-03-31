"""Smoke tests for position router."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from positions.position_router import get_calibration, router
from fastapi import FastAPI
from positions.probability_model import ProbabilityModel

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)

class TestRouterMounts:
    def test_packages_endpoint_exists(self, client):
        r = client.get("/api/derivatives/packages")
        assert r.status_code in (200, 503)  # 503 if position system not init'd
    def test_config_endpoint(self, client):
        r = client.get("/api/derivatives/config")
        assert r.status_code == 200
    def test_dashboard_endpoint(self, client):
        r = client.get("/api/derivatives/dashboard")
        assert r.status_code in (200, 503)

    def test_calibration_endpoint_returns_report_when_engine_attached(self):
        app = FastAPI()
        app.state.consensus_calibration_engine = ProbabilityModel()
        app.state.consensus_calibration_engine.update_from_matched_events([{
            "canonical_title": "BTC > 100k",
            "category": "crypto",
            "expiry": "2026-12-31",
            "markets": [
                {"platform": "polymarket", "yes_price": 0.60, "volume": 100000},
                {"platform": "kalshi", "yes_price": 0.55, "volume": 50000},
            ],
        }])
        app.include_router(router)
        local_client = TestClient(app)
        r = local_client.get("/api/derivatives/calibration")
        assert r.status_code == 200
        payload = r.json()
        assert payload["tracked_events"] == 1
        assert payload["tracked_buckets"] >= 2

    def test_calibration_endpoint_combines_threshold_and_consensus_reports(self):
        class DummyThresholdEngine:
            def generate_report(self):
                return {"trade_count": 5}

        consensus = ProbabilityModel()
        consensus.update_from_matched_events([{
            "canonical_title": "ETH > 4k",
            "category": "crypto",
            "expiry": "2026-12-31",
            "markets": [
                {"platform": "polymarket", "yes_price": 0.30, "volume": 100000},
                {"platform": "kalshi", "yes_price": 0.50, "volume": 100000},
            ],
        }])
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    calibration_engine=DummyThresholdEngine(),
                    consensus_calibration_engine=consensus,
                )
            )
        )
        payload = asyncio.run(get_calibration(request))
        assert payload["threshold_calibration"]["trade_count"] == 5
        assert payload["consensus_calibration"]["tracked_events"] == 1
