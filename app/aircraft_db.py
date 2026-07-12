import asyncio
import gzip
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Data directory is at server/data/ (sibling of server/app/)
DB_DIR = Path(os.path.dirname(os.path.dirname(__file__))) / "data"
DB_FILE = DB_DIR / "basic-ac-db.json.gz"
META_FILE = DB_DIR / "db_meta.json"
DB_URL = "https://downloads.adsbexchange.com/downloads/basic-ac-db.json.gz"
HEXDB_API = "https://hexdb.io/api/v1/aircraft"

STALE_DAYS = 7
REFRESH_INTERVAL_SECONDS = 6 * 60 * 60  # 6 hours

# Minimum time between *manually triggered* refreshes (POST /api/db/update).
# Without this, any client holding a valid API key could repeatedly force
# multi-megabyte downloads from adsbexchange.com.
MANUAL_REFRESH_COOLDOWN_SECONDS = 5 * 60


class AircraftDB:
    """Aircraft metadata database backed by a local ADS-B Exchange snapshot
    with an online hexdb.io API fallback for cache misses."""

    def __init__(self):
        self._db: dict[str, dict[str, Any]] = {}
        self._api_cache: dict[str, dict[str, Any] | None] = {}
        self._loaded = False
        self._meta: dict[str, Any] = {}
        self._refresh_task: asyncio.Task | None = None
        self._http: httpx.AsyncClient | None = None
        self._last_manual_refresh: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Load the local DB (downloading if needed) and start the refresh timer."""
        DB_DIR.mkdir(parents=True, exist_ok=True)
        self._http = httpx.AsyncClient(timeout=60)

        if not DB_FILE.exists():
            logger.info("Aircraft database not found — downloading...")
            await self._download()

        self._load_from_disk()

        if self._is_stale():
            logger.info("Aircraft database is stale (%s days old) — refreshing in background",
                        round(self._age_days(), 1))
            asyncio.create_task(self._background_download_and_reload())

        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop(self):
        """Cancel the refresh timer and close the HTTP client."""
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        if self._http:
            await self._http.aclose()

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    async def lookup(self, icao: str) -> dict[str, Any] | None:
        """Look up aircraft metadata by ICAO hex code.
        Returns a dict with normalized field names, or None if unknown."""
        icao_upper = icao.upper()

        # Tier 1: local database
        entry = self._db.get(icao_upper)
        if entry is not None:
            return self._normalize_local(entry)

        # Tier 2: in-memory API cache (includes negative results)
        if icao_upper in self._api_cache:
            return self._api_cache[icao_upper]

        # Tier 2: hexdb.io API
        result = await self._fetch_hexdb(icao_upper)
        self._api_cache[icao_upper] = result
        return result

    # ------------------------------------------------------------------
    # Manual refresh
    # ------------------------------------------------------------------

    async def refresh(self) -> dict[str, Any]:
        """Force re-download and hot-reload the database. Returns status info.

        Rate-limited to at most once per ``MANUAL_REFRESH_COOLDOWN_SECONDS``
        to prevent a client from repeatedly triggering multi-MB downloads.
        """
        now = time.time()
        elapsed = now - self._last_manual_refresh
        if elapsed < MANUAL_REFRESH_COOLDOWN_SECONDS:
            return {
                "status": "cooldown",
                "message": "Manual refresh was requested too recently",
                "retry_after_seconds": round(MANUAL_REFRESH_COOLDOWN_SECONDS - elapsed, 1),
                "aircraft_count": len(self._db),
                "downloaded_at": self._meta.get("downloaded_at"),
            }

        self._last_manual_refresh = now
        previous_age = self._age_days()
        await self._download()
        self._load_from_disk()
        return {
            "status": "updated",
            "aircraft_count": len(self._db),
            "downloaded_at": self._meta.get("downloaded_at"),
            "previous_age_days": round(previous_age, 1) if previous_age is not None else None,
        }

    def has_icao(self, icao: str) -> bool:
        """Check if an ICAO hex code exists in the local database."""
        return icao.upper() in self._db

    def get_status(self) -> dict[str, Any]:
        """Return current database status for the /api/db/status endpoint."""
        age = self._age_days()
        return {
            "loaded": self._loaded,
            "aircraft_count": len(self._db),
            "downloaded_at": self._meta.get("downloaded_at"),
            "age_days": round(age, 1) if age is not None else None,
            "is_stale": self._is_stale(),
            "source": "adsbexchange",
        }

    # ------------------------------------------------------------------
    # Internals — download
    # ------------------------------------------------------------------

    async def _download(self):
        """Download the basic-ac-db.json.gz from ADS-B Exchange."""
        try:
            logger.info("Downloading aircraft database from %s ...", DB_URL)
            if self._http is None:
                self._http = httpx.AsyncClient(timeout=60)

            resp = await self._http.get(DB_URL, follow_redirects=True)
            resp.raise_for_status()

            DB_DIR.mkdir(parents=True, exist_ok=True)
            DB_FILE.write_bytes(resp.content)

            count = self._count_entries(resp.content)

            meta = {
                "downloaded_at": datetime.now(timezone.utc).isoformat(),
                "source_url": DB_URL,
                "file_size_bytes": len(resp.content),
                "aircraft_count": count,
            }
            META_FILE.write_text(json.dumps(meta, indent=2))
            self._meta = meta

            logger.info("Aircraft database downloaded: %d entries, %.1f MB",
                        count, len(resp.content) / 1_048_576)
        except Exception:
            logger.exception("Failed to download aircraft database")

    @staticmethod
    def _count_entries(gz_bytes: bytes) -> int:
        """Count aircraft entries in the gzipped JSON payload."""
        try:
            raw = gzip.decompress(gz_bytes)
            text = raw.decode("utf-8", errors="replace")
            try:
                data = json.loads(text)
                if isinstance(data, list):
                    return len(data)
                if isinstance(data, dict):
                    return len(data)
                return 0
            except json.JSONDecodeError:
                return sum(1 for line in text.splitlines() if line.strip())
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Internals — load
    # ------------------------------------------------------------------

    def _load_from_disk(self):
        """Load the gzipped database into the in-memory dict."""
        if not DB_FILE.exists():
            logger.warning("Aircraft database file not found at %s", DB_FILE)
            self._loaded = False
            return

        try:
            with gzip.open(DB_FILE, "rb") as f:
                raw = f.read()

            db: dict[str, dict[str, Any]] = {}
            text = raw.decode("utf-8", errors="replace")

            try:
                data = json.loads(text)
                if isinstance(data, list):
                    for entry in data:
                        icao = entry.get("icao") or entry.get("hex") or entry.get("ModeS")
                        if icao:
                            db[icao.upper()] = entry
                elif isinstance(data, dict):
                    for key, entry in data.items():
                        if isinstance(entry, dict):
                            db[key.upper()] = entry
                        else:
                            db[key.upper()] = {"icao": key}
            except json.JSONDecodeError:
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        icao = entry.get("icao") or entry.get("hex") or entry.get("ModeS")
                        if icao:
                            db[icao.upper()] = entry
                    except json.JSONDecodeError:
                        continue

            self._db = db
            self._loaded = True

            if META_FILE.exists():
                try:
                    self._meta = json.loads(META_FILE.read_text())
                except Exception:
                    self._meta = {}

            if self._meta:
                self._meta["aircraft_count"] = len(db)

            logger.info("Aircraft database loaded: %d entries", len(self._db))
        except Exception:
            logger.exception("Failed to load aircraft database from %s", DB_FILE)
            self._loaded = False

    # ------------------------------------------------------------------
    # Internals — hexdb.io fallback
    # ------------------------------------------------------------------

    async def _fetch_hexdb(self, icao: str) -> dict[str, Any] | None:
        """Query the hexdb.io API for a single ICAO hex code."""
        try:
            if self._http is None:
                self._http = httpx.AsyncClient(timeout=60)

            resp = await self._http.get(f"{HEXDB_API}/{icao}", follow_redirects=True)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "404" or data.get("error"):
                return None
            return self._normalize_hexdb(data)
        except Exception:
            logger.debug("hexdb.io lookup failed for %s", icao, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Internals — normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_local(entry: dict) -> dict[str, Any]:
        """Normalize field names from the ADS-B Exchange DB format."""
        mil_raw = entry.get("mil")
        is_military = None
        if isinstance(mil_raw, bool):
            is_military = mil_raw
        elif isinstance(mil_raw, str):
            is_military = mil_raw.lower() in ("1", "true", "yes", "y")
        elif isinstance(mil_raw, (int, float)):
            is_military = bool(mil_raw)

        return {
            "registration": entry.get("reg") or None,
            "aircraft_type": entry.get("icaotype") or entry.get("short_type") or None,
            "aircraft_model": entry.get("model") or None,
            "manufacturer": entry.get("manufacturer") or None,
            "owner_operator": entry.get("ownop") or None,
            "year_built": str(entry["year"]) if entry.get("year") else None,
            "is_military": is_military,
        }

    @staticmethod
    def _normalize_hexdb(data: dict) -> dict[str, Any]:
        """Normalize field names from the hexdb.io API response."""
        return {
            "registration": data.get("Registration") or None,
            "aircraft_type": data.get("ICAOTypeCode") or None,
            "aircraft_model": data.get("Type") or None,
            "manufacturer": data.get("Manufacturer") or None,
            "owner_operator": data.get("RegisteredOwners") or None,
            "year_built": None,
            "is_military": None,
        }

    # ------------------------------------------------------------------
    # Internals — freshness
    # ------------------------------------------------------------------

    def _age_days(self) -> float | None:
        """Return the age of the database in days, or None if unknown."""
        ts = self._meta.get("downloaded_at")
        if not ts:
            if DB_FILE.exists():
                mtime = os.path.getmtime(DB_FILE)
                return (time.time() - mtime) / 86400
            return None
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except Exception:
            return None

    def _is_stale(self) -> bool:
        """Check whether the database is older than STALE_DAYS."""
        age = self._age_days()
        if age is None:
            return True
        return age > STALE_DAYS

    async def _background_download_and_reload(self):
        """Download a new database copy and hot-reload it into memory."""
        try:
            await self._download()
            self._load_from_disk()
        except Exception:
            logger.exception("Background database refresh failed")

    async def _refresh_loop(self):
        """Periodically check database age and refresh if stale."""
        while True:
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
            try:
                if self._is_stale():
                    logger.info("Scheduled refresh: database is stale, downloading...")
                    await self._background_download_and_reload()
                else:
                    logger.debug("Scheduled refresh: database is fresh (%.1f days old)",
                                 self._age_days() or 0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error during scheduled database refresh")
