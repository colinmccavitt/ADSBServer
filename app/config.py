"""Persistent JSON configuration for the ADS-B server."""

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")

DEFAULTS: dict[str, Any] = {
    "latitude": 38.85596396471333,
    "longitude": -77.04951658878798,
    "http_port": 8080,
    "collector_port": 4002,
}


def load() -> dict[str, Any]:
    """Load config from disk, falling back to defaults for missing keys."""
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
    return config


def save(config: dict[str, Any]) -> None:
    """Write config to disk."""
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
        logger.info("Saved config to %s", CONFIG_PATH)
    except Exception:
        logger.exception("Failed to write %s", CONFIG_PATH)
