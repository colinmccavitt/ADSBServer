"""Tests for app.auth — API key validation and the require_api_key
FastAPI dependency, including a regression test for the Origin/Referer
substring-match bypass fixed in this pass."""

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


def test_rejects_request_with_no_key_and_no_matching_origin():
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


def test_browser_exemption_allows_exact_origin_match():
    _set_keys(client_keys={"good-key"})
    client = TestClient(_make_protected_app(), base_url="http://testserver")
    resp = client.get("/protected", headers={"Origin": "http://testserver"})
    assert resp.status_code == 200


def test_browser_exemption_allows_exact_referer_match():
    _set_keys(client_keys={"good-key"})
    client = TestClient(_make_protected_app(), base_url="http://testserver")
    resp = client.get("/protected", headers={"Referer": "http://testserver/"})
    assert resp.status_code == 200


@pytest.mark.parametrize("spoofed_origin", [
    "http://testserver.evil.net",
    "http://evil-testserver",
    "http://evil.com/?x=testserver",
])
def test_substring_spoofed_origin_is_rejected(spoofed_origin):
    """Regression test: a domain that merely *contains* the server's host
    as a substring must NOT be treated as same-origin. Before the fix,
    `server_host in header_val` allowed exactly this bypass."""
    _set_keys(client_keys={"good-key"})
    client = TestClient(_make_protected_app(), base_url="http://testserver")
    resp = client.get("/protected", headers={"Origin": spoofed_origin})
    assert resp.status_code == 401
