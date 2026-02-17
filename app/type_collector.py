"""Collects unique aircraft types and models detected by the receiver.

Every time an aircraft is enriched with type/model metadata, this module
records the unique combination and persists it to data/aircraft_types.json.
The file accumulates across restarts so it becomes a growing catalogue of
every aircraft type ever seen by the receiver.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Data directory is at server/data/ (sibling of server/app/)
TYPES_FILE = Path(os.path.dirname(os.path.dirname(__file__))) / "data" / "aircraft_types.json"


class TypeCollector:
    """Tracks unique aircraft type/model combinations and persists them to disk."""

    def __init__(self):
        self._types: dict[str, dict[str, Any]] = {}
        self._dirty = False

    def load(self):
        """Load previously saved types from disk."""
        if TYPES_FILE.exists():
            try:
                data = json.loads(TYPES_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "types" in data:
                    for entry in data["types"]:
                        key = self._make_key(entry.get("icao_type"), entry.get("model"))
                        if key:
                            self._types[key] = entry
                logger.info("Type collector loaded %d unique types from disk", len(self._types))
            except Exception:
                logger.exception("Failed to load aircraft types file")

    def record(self, enrichment: dict[str, Any]):
        """Record a type/model from aircraft enrichment data."""
        icao_type = enrichment.get("aircraft_type")
        model = enrichment.get("aircraft_model")

        if not icao_type and not model:
            return

        key = self._make_key(icao_type, model)
        if not key:
            return

        airline = enrichment.get("owner_operator") or None

        if key in self._types:
            self._types[key]["times_seen"] = self._types[key].get("times_seen", 1) + 1
            self._types[key]["last_seen"] = datetime.now(timezone.utc).isoformat()
            if airline:
                airlines = self._types[key].get("airlines", [])
                if airline not in airlines:
                    airlines.append(airline)
                    self._types[key]["airlines"] = airlines
            self._dirty = True
            return

        entry: dict[str, Any] = {
            "icao_type": icao_type,
            "model": model,
            "manufacturer": enrichment.get("manufacturer"),
            "airlines": [airline] if airline else [],
            "is_military": enrichment.get("is_military"),
            "first_seen": datetime.now(timezone.utc).isoformat(),
            "last_seen": datetime.now(timezone.utc).isoformat(),
            "times_seen": 1,
        }

        self._types[key] = entry
        self._dirty = True
        logger.info(
            "New aircraft type recorded: %s / %s (%s) — %d unique types total",
            icao_type or "—",
            model or "—",
            enrichment.get("manufacturer") or "unknown",
            len(self._types),
        )

    def save(self):
        """Write the current type catalogue to disk (if changed)."""
        if not self._dirty:
            return

        TYPES_FILE.parent.mkdir(parents=True, exist_ok=True)

        sorted_types = sorted(
            self._types.values(),
            key=lambda t: (t.get("manufacturer") or "", t.get("icao_type") or "", t.get("model") or ""),
        )

        data = {
            "description": "Unique aircraft types and models detected by this ADS-B receiver",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "unique_type_count": len(sorted_types),
            "types": sorted_types,
        }

        try:
            TYPES_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            self._dirty = False
            logger.debug("Saved %d aircraft types to %s", len(sorted_types), TYPES_FILE)
        except Exception:
            logger.exception("Failed to save aircraft types file")

    def get_types(self) -> list[dict[str, Any]]:
        """Return all collected types sorted by manufacturer then type code."""
        return sorted(
            self._types.values(),
            key=lambda t: (t.get("manufacturer") or "", t.get("icao_type") or "", t.get("model") or ""),
        )

    def get_summary(self) -> dict[str, Any]:
        """Return summary statistics about the collected types."""
        types_list = self.get_types()
        manufacturers = {t.get("manufacturer") for t in types_list if t.get("manufacturer")}
        icao_types = {t.get("icao_type") for t in types_list if t.get("icao_type")}
        military_count = sum(1 for t in types_list if t.get("is_military"))

        airlines = set()
        for t in types_list:
            for a in t.get("airlines", []):
                airlines.add(a)

        return {
            "unique_type_model_combinations": len(types_list),
            "unique_icao_type_codes": len(icao_types),
            "unique_manufacturers": len(manufacturers),
            "unique_airlines": len(airlines),
            "military_types": military_count,
            "file": str(TYPES_FILE),
        }

    @staticmethod
    def _make_key(icao_type: str | None, model: str | None) -> str | None:
        """Create a unique key from type code and model."""
        t = (icao_type or "").strip().upper()
        m = (model or "").strip().upper()
        if not t and not m:
            return None
        return f"{t}||{m}"
