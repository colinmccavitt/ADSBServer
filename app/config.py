"""Persistent JSON configuration for the ADS-B server.

API keys live in a *separate* file (``config.secrets.json``) that is
excluded from version control via ``.gitignore``. This keeps credentials
out of ``config.json`` — which is generally safe to commit — and out of
anything ``save()`` writes back to disk (e.g. via ``PUT /api/config``).
"""

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(_BASE_DIR, "config.json")
SECRETS_PATH = os.path.join(_BASE_DIR, "config.secrets.json")

DEFAULTS: dict[str, Any] = {
    "latitude": 38.85596396471333,
    "longitude": -77.04951658878798,
    "http_port": 8080,
    "collector_port": 4002,
}

# Keys that must never be written back to CONFIG_PATH (they belong only in
# the gitignored secrets file). Stripped defensively in save().
_SECRET_KEYS = ("api_keys",)


def load() -> dict[str, Any]:
    """Load config from disk, falling back to defaults for missing keys.

    Does **not** include API keys — use :func:`load_secrets` for those.
    """
    config = dict(DEFAULTS)
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                saved = json.load(f)
            config.update(saved)
            logger.info("Loaded config from %s", CONFIG_PATH)
        except Exception:
            logger.exception("Failed to read %s, using defaults", CONFIG_PATH)
    else:
        logger.info("No config.json found, using defaults")
    for key in _SECRET_KEYS:
        config.pop(key, None)
    return config


def load_secrets() -> dict[str, Any]:
    """Load API keys from the gitignored secrets file.

    Returns an empty dict (open access, backward compatible) if the file
    does not exist. Falls back to an ``api_keys`` block in ``config.json``
    if present, for compatibility with older config layouts — but new
    deployments should use ``config.secrets.json`` exclusively.
    """
    if os.path.isfile(SECRETS_PATH):
        try:
            with open(SECRETS_PATH, "r") as f:
                secrets = json.load(f)
            logger.info("Loaded API keys from %s", SECRETS_PATH)
            return secrets
        except Exception:
            logger.exception("Failed to read %s", SECRETS_PATH)
            return {}

    # Legacy fallback: api_keys embedded directly in config.json.
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                saved = json.load(f)
            legacy = saved.get("api_keys")
            if legacy:
                logger.warning(
                    "Loading API keys from config.json — move them to %s "
                    "so they are not tracked by version control",
                    SECRETS_PATH,
                )
                return legacy
        except Exception:
            pass
    return {}


def save(config: dict[str, Any]) -> None:
    """Write config to disk. Never persists secret keys (see _SECRET_KEYS)."""
    to_write = {k: v for k, v in config.items() if k not in _SECRET_KEYS}
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(to_write, f, indent=2)
        logger.info("Saved config to %s", CONFIG_PATH)
    except Exception:
        logger.exception("Failed to write %s", CONFIG_PATH)
