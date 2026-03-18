"""Smoke tests for position router."""
import pytest
from fastapi.testclient import TestClient
from positions.position_router import router
from fastapi import FastAPI

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
