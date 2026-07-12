"""Smoke tests for the FastAPI app's HTTP surface.

Uses a bare TestClient(app) (no ``with`` block) so the app's lifespan
(which would start the collector TCP hub and attempt to download the
aircraft database over the network) does not run — these tests only
exercise the HTTP routing/auth layer, not the background services.
"""

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def _open_access():
    auth._collector_keys = set()
    auth._client_keys = set()


def _require_client_key(key: str):
    auth._collector_keys = set()
    auth._client_keys = {key}


def test_index_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_3d_page_serves_html(client):
    resp = client.get("/3d")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_admin_page_serves_html_without_auth(client):
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_watchlist_endpoint_requires_no_auth(client):
    _require_client_key("secret")
    resp = client.get("/api/watchlist")
    assert resp.status_code == 200
    assert "watchlist" in resp.json()


def test_aircraft_list_open_access_when_no_keys_configured(client):
    _open_access()
    resp = client.get("/api/aircraft")
    assert resp.status_code == 200
    body = resp.json()
    assert "count" in body
    assert "aircraft" in body


def test_aircraft_list_requires_key_when_configured(client):
    _require_client_key("secret-key")
    resp = client.get("/api/aircraft")
    assert resp.status_code == 401

    resp = client.get("/api/aircraft", headers={"X-API-Key": "secret-key"})
    assert resp.status_code == 200


def test_stats_endpoint(client):
    _open_access()
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert "uptime_seconds" in body
    assert "aircraft_count" in body


def test_collectors_list_empty_when_hub_not_started(client):
    """Without the lifespan running, collector_hub stays None — the route
    should degrade gracefully to an empty list rather than error."""
    _open_access()
    resp = client.get("/api/collectors")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_unknown_collector_returns_error_payload(client):
    _open_access()
    resp = client.get("/api/collectors/does-not-exist")
    assert resp.status_code == 200
    assert "error" in resp.json()


def test_get_unknown_aircraft_returns_error_payload(client):
    _open_access()
    resp = client.get("/api/aircraft/FFFFFF")
    assert resp.status_code == 200
    assert "error" in resp.json()


def test_config_roundtrip(client, tmp_path, monkeypatch):
    """PUT /api/config writes through to disk via cfg.save(); redirect
    CONFIG_PATH to a temp file so the test doesn't mutate (and reformat)
    the repo's real config.json. Also restore the shared in-memory
    store.receiver_lat/lon afterwards since PUT mutates that global."""
    import app.config as cfg
    from app.main import store

    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    _open_access()
    original_lat, original_lon = store.receiver_lat, store.receiver_lon

    try:
        put_resp = client.put("/api/config", json={"latitude": 12.34, "longitude": 56.78})
        assert put_resp.status_code == 200
        assert put_resp.json() == {"latitude": 12.34, "longitude": 56.78}

        get_resp = client.get("/api/config")
        assert get_resp.status_code == 200
        assert get_resp.json()["latitude"] == 12.34
        assert get_resp.json()["longitude"] == 56.78
    finally:
        store.receiver_lat, store.receiver_lon = original_lat, original_lon
