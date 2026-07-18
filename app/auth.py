"""API key authentication for collectors and API clients.

Provides validation helpers that check keys against the ``api_keys``
section in ``config.secrets.json``.  When the section is absent or empty,
authentication is disabled for backward compatibility.

Browser UI auth uses an HttpOnly SameSite cookie set when serving HTML
pages — never a spoofable Origin/Referer/Host match (AUTH-001).
"""

import logging
from typing import Any

from fastapi import Header, HTTPException, Request, WebSocket
from starlette.responses import Response

from app import config as cfg

logger = logging.getLogger(__name__)

# Cookie set on HTML page responses so same-origin fetch/WS carry a real key.
BROWSER_COOKIE_NAME = "adsb_api_key"

# Cached key sets (populated on first access / reload)
_collector_keys: set[str] | None = None
_client_keys: set[str] | None = None


def _load_keys() -> None:
    """Load API keys from the gitignored secrets file into module-level caches."""
    global _collector_keys, _client_keys

    api_keys: dict[str, Any] = cfg.load_secrets()

    raw_collector = api_keys.get("collector_keys", [])
    raw_client = api_keys.get("client_keys", [])

    _collector_keys = set(raw_collector) if raw_collector else set()
    _client_keys = set(raw_client) if raw_client else set()

    logger.info(
        "Loaded API keys: %d collector key(s), %d client key(s)",
        len(_collector_keys),
        len(_client_keys),
    )


def reload_keys() -> None:
    """Force-reload keys from disk (e.g. after config change)."""
    _load_keys()


def _ensure_loaded() -> None:
    if _collector_keys is None or _client_keys is None:
        _load_keys()


def validate_collector_key(key: str | None) -> bool:
    """Return True if *key* is a valid collector API key.

    If no collector keys are configured, all collectors are accepted
    (backward-compatible behaviour).
    """
    _ensure_loaded()
    assert _collector_keys is not None
    if not _collector_keys:
        return True  # no keys configured — open access
    if not key:
        return False
    return key in _collector_keys


def validate_client_key(key: str | None) -> bool:
    """Return True if *key* is a valid client API key.

    If no client keys are configured, all clients are accepted.
    """
    _ensure_loaded()
    assert _client_keys is not None
    if not _client_keys:
        return True  # no keys configured — open access
    if not key:
        return False
    return key in _client_keys


def client_keys_configured() -> bool:
    """Return True if at least one client key is configured."""
    _ensure_loaded()
    assert _client_keys is not None
    return len(_client_keys) > 0


def browser_session_key() -> str | None:
    """Stable client key to stamp into the browser session cookie.

    Returns None when client auth is open (no keys configured).
    """
    _ensure_loaded()
    assert _client_keys is not None
    if not _client_keys:
        return None
    return sorted(_client_keys)[0]


def attach_browser_session_cookie(response: Response) -> None:
    """Set the HttpOnly session cookie on an HTML response when keys exist."""
    key = browser_session_key()
    if key is None:
        return
    response.set_cookie(
        key=BROWSER_COOKIE_NAME,
        value=key,
        httponly=True,
        samesite="strict",
        path="/",
        max_age=60 * 60 * 24 * 30,  # 30 days
    )


def _extract_client_key(
    *,
    x_api_key: str | None,
    cookie_key: str | None,
) -> str | None:
    if x_api_key:
        return x_api_key
    if cookie_key:
        return cookie_key
    return None


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(None),
) -> None:
    """FastAPI dependency that enforces API key auth on ``/api/*`` routes.

    Rules:
      - If no client keys are configured, all requests pass (open access).
      - If a valid ``X-API-Key`` header is present, the request passes.
      - If a valid ``adsb_api_key`` cookie is present (set when serving the
        HTML UI), the request passes. The cookie value is a real configured
        key — spoofing Host/Origin/Referer alone is not enough (AUTH-001).
      - Otherwise the request is rejected with 401.
    """
    _ensure_loaded()
    assert _client_keys is not None

    if not _client_keys:
        return

    key = _extract_client_key(
        x_api_key=x_api_key,
        cookie_key=request.cookies.get(BROWSER_COOKIE_NAME),
    )
    if key and key in _client_keys:
        return

    raise HTTPException(
        status_code=401,
        detail="Valid API key required. Pass via X-API-Key header.",
    )


async def accept_websocket_if_authorized(ws: WebSocket) -> bool:
    """Accept a WebSocket only when client auth passes.

    Returns True if the socket was accepted and authorized. On failure the
    socket is closed with policy-violation after accept (Starlette requires
    accept before a typed close on some paths).
    """
    await ws.accept()
    _ensure_loaded()
    assert _client_keys is not None
    if not _client_keys:
        return True

    header_key = ws.headers.get("x-api-key")
    cookie_key = ws.cookies.get(BROWSER_COOKIE_NAME)
    key = _extract_client_key(x_api_key=header_key, cookie_key=cookie_key)
    if key and key in _client_keys:
        return True

    await ws.close(code=1008)
    return False
