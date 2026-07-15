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


# ---------------------------------------------------------------------------
# Structured push ingest (WebSocket /ingest)
# ---------------------------------------------------------------------------

def _require_collector_key(key: str):
    auth._client_keys = set()
    auth._collector_keys = {key}


def test_ingest_rejects_bad_api_key(client):
    _require_collector_key("good-collector-key")
    with client.websocket_connect("/ingest") as ws:
        ws.send_json({"id": "c1", "name": "test", "lat": 0, "lon": 0,
                      "api_key": "wrong"})
        assert ws.receive_json() == {"error": "invalid_api_key"}


def test_ingest_accepts_valid_hello(client):
    _require_collector_key("good-collector-key")
    with client.websocket_connect("/ingest") as ws:
        ws.send_json({"id": "c1", "name": "test", "lat": 0, "lon": 0,
                      "api_key": "good-collector-key"})
        reply = ws.receive_json()
        assert reply["ok"] is True
        assert reply["id"] == "c1"


def test_ingest_stores_preprocessed_attitude(client, monkeypatch):
    """A pushed aircraft is stored verbatim with preprocessed attitude and
    served on /api/aircraft — no 25 ft snap, no re-smoothing."""
    from app.main import store

    _open_access()
    # Disable enrichment so store.update() never reaches the network.
    monkeypatch.setattr(store, "_aircraft_db", None)

    sample = {
        "icao": "ABC123",
        "callsign": "TEST01",
        "altitude": 35007,          # deliberately not a 25 ft multiple
        "alt_geom": 34897,
        "geo_minus_baro": -110,
        "ground_speed": 450.5,
        "track": 270.0,
        "heading": 268.5,
        "latitude": 38.856,
        "longitude": -77.0495,
        "vertical_rate": -512,
        "roll_deg": -12.4,
        "pitch_deg": 2.1,
        "gamma_deg": -3.0,
    }

    with client.websocket_connect("/ingest") as ws:
        ws.send_json({"id": "pusher", "name": "test", "lat": 0, "lon": 0,
                      "api_key": ""})
        assert ws.receive_json()["ok"] is True
        ws.send_json({"aircraft": [sample]})

    resp = client.get("/api/aircraft/ABC123")
    assert resp.status_code == 200
    body = resp.json()
    assert body["icao"] == "ABC123"
    assert body["preprocessed"] is True
    # Preserved verbatim (not snapped to the nearest 25 ft).
    assert body["altitude"] == 35007
    assert body["heading"] == 268.5
    assert body["roll_deg"] == -12.4
    assert body["pitch_deg"] == 2.1
    assert body["gamma_deg"] == -3.0
    assert body["geo_minus_baro"] == -110
