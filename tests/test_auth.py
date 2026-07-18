"""Tests for app.auth — API key validation and the require_api_key
FastAPI dependency, including AUTH-001 regression (Host/Origin spoof
must not bypass the key requirement).
"""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import auth


def _set_keys(*, collector_keys=frozenset(), client_keys=frozenset()):
    """Set both module-level caches so _ensure_loaded() won't reload from
    the real (gitignored) secrets file mid-test."""
    auth._collector_keys = set(collector_keys)
    auth._client_keys = set(client_keys)


def _make_protected_app() -> FastAPI:
    test_app = FastAPI()

    @test_app.get("/protected", dependencies=[Depends(auth.require_api_key)])
    def protected():
        return {"ok": True}

    return test_app


# ---------------------------------------------------------------------
# validate_collector_key / validate_client_key
# ---------------------------------------------------------------------

def test_collector_key_open_access_when_none_configured():
    _set_keys()
    assert auth.validate_collector_key(None) is True
    assert auth.validate_collector_key("anything") is True


def test_collector_key_rejects_when_configured_and_missing():
    _set_keys(collector_keys={"good-key"})
    assert auth.validate_collector_key(None) is False
    assert auth.validate_collector_key("wrong-key") is False


def test_collector_key_accepts_valid_key():
    _set_keys(collector_keys={"good-key"})
    assert auth.validate_collector_key("good-key") is True


def test_client_keys_configured_flag():
    _set_keys(client_keys=set())
    assert auth.client_keys_configured() is False
    _set_keys(client_keys={"k"})
    assert auth.client_keys_configured() is True


# ---------------------------------------------------------------------
# require_api_key dependency (via a throwaway FastAPI app)
# ---------------------------------------------------------------------

def test_open_access_when_no_client_keys_configured():
    _set_keys(client_keys=set())
    client = TestClient(_make_protected_app())
    resp = client.get("/protected")
    assert resp.status_code == 200


def test_rejects_request_with_no_key_and_no_cookie():
    _set_keys(client_keys={"good-key"})
    client = TestClient(_make_protected_app())
    resp = client.get("/protected")
    assert resp.status_code == 401


def test_accepts_valid_x_api_key_header():
    _set_keys(client_keys={"good-key"})
    client = TestClient(_make_protected_app())
    resp = client.get("/protected", headers={"X-API-Key": "good-key"})
    assert resp.status_code == 200


def test_rejects_wrong_x_api_key_header():
    _set_keys(client_keys={"good-key"})
    client = TestClient(_make_protected_app())
    resp = client.get("/protected", headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 401


def test_accepts_valid_session_cookie():
    _set_keys(client_keys={"good-key"})
    client = TestClient(_make_protected_app())
    resp = client.get(
        "/protected", cookies={auth.BROWSER_COOKIE_NAME: "good-key"}
    )
    assert resp.status_code == 200


def test_rejects_wrong_session_cookie():
    _set_keys(client_keys={"good-key"})
    client = TestClient(_make_protected_app())
    resp = client.get(
        "/protected", cookies={auth.BROWSER_COOKIE_NAME: "wrong-key"}
    )
    assert resp.status_code == 401


@pytest.mark.parametrize("spoofed_origin", [
    "http://testserver",
    "http://testserver.evil.net",
    "http://evil-testserver",
    "http://evil.com/?x=testserver",
])
def test_origin_referer_exemption_removed(spoofed_origin):
    """AUTH-001: matching Host+Origin/Referer must NOT authorize without a key.

    Before the fix, any client that set Origin to match Host bypassed
    X-API-Key entirely. The browser UI now authenticates via the
    ``adsb_api_key`` cookie (a real configured key), not header spoofing.
    """
    _set_keys(client_keys={"good-key"})
    client = TestClient(_make_protected_app(), base_url="http://testserver")
    resp = client.get(
        "/protected",
        headers={
            "Host": "testserver",
            "Origin": spoofed_origin,
            "Referer": spoofed_origin + "/",
        },
    )
    assert resp.status_code == 401


def test_browser_session_key_is_stable_sorted_first():
    _set_keys(client_keys={"zeta", "alpha"})
    assert auth.browser_session_key() == "alpha"
