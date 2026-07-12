"""API key authentication for collectors and API clients.

Provides validation helpers that check keys against the ``api_keys``
section in ``config.json``.  When the section is absent or empty,
authentication is disabled for backward compatibility.
"""

import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request

from app import config as cfg

logger = logging.getLogger(__name__)

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


def _same_host(header_val: str, server_host: str) -> bool:
    """Return True if *header_val* (a Referer/Origin URL) has exactly the
    same ``host:port`` as *server_host* (the request's own Host header).

    Uses proper URL parsing rather than substring containment, which is
    spoofable by any domain that happens to contain ``server_host`` as a
    substring.
    """
    try:
        parsed_host = urlsplit(header_val).netloc
    except ValueError:
        return False
    return bool(parsed_host) and parsed_host.lower() == server_host.lower()


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(None),
) -> None:
    """FastAPI dependency that enforces API key auth on ``/api/*`` routes.

    Rules:
      - If no client keys are configured, all requests pass (open access).
      - If a valid ``X-API-Key`` header is present, the request passes.
      - If the request originates from the server's own browser UI
        (``Referer`` header matches the server origin), the request passes
        without a key — this lets the browser frontend call ``/api/*``
        seamlessly.
      - Otherwise the request is rejected with 401.
    """
    _ensure_loaded()
    assert _client_keys is not None

    # No client keys configured — open access (backward compatible)
    if not _client_keys:
        return

    # Valid API key provided
    if x_api_key and x_api_key in _client_keys:
        return

    # Browser UI exemption: check Referer / Origin header
    referer = request.headers.get("referer", "")
    origin = request.headers.get("origin", "")
    server_host = request.headers.get("host", "")

    if server_host:
        # Accept only if the referer/origin's host:port exactly matches this
        # server. A naive substring check (`server_host in header_val`) can
        # be spoofed by any domain that merely *contains* the server's host
        # as a substring (e.g. "server.example.com.evil.net"), so we parse
        # the header and compare hosts exactly instead.
        for header_val in (referer, origin):
            if header_val and _same_host(header_val, server_host):
                return

    raise HTTPException(
        status_code=401,
        detail="Valid API key required. Pass via X-API-Key header.",
    )
