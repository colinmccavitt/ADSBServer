"""SQLite-backed ICAO -> aircraft-metadata index.

The ADS-B Exchange snapshot holds ~616k aircraft. Held as a Python dict of
dicts that is ~1.1 GB resident - 28% of a 4 GB server - purely to serve point
lookups on a primary key. Measured on the live server:

    RES 1.1g / VmData 1.2g, for 615,656 entries of 11 keys each

SQLite stores the same data in tens of megabytes on disk with a few MB of page
cache, and an indexed point lookup costs microseconds. Against a ~240 msg/s
feed (``has_icao`` runs once per message) that is a rounding error, so the
memory is recovered essentially for free.

Only the seven fields the server actually exposes are stored; the snapshot's
remaining columns were being held in memory and then discarded on every read.

The index is a *cache*: it is rebuilt whenever the source snapshot changes, and
callers are expected to fall back to an in-memory dict if it cannot be built.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator

logger = logging.getLogger(__name__)

# Bump when the stored column set changes so existing caches rebuild.
SCHEMA_VERSION = 1

_COLUMNS = (
    "registration",
    "aircraft_type",
    "aircraft_model",
    "manufacturer",
    "owner_operator",
    "year_built",
    "is_military",
)


class AircraftIndex:
    """Point-lookup index over the aircraft snapshot, backed by SQLite."""

    def __init__(self, path: Path):
        self.path = path
        # check_same_thread=False so a rebuild can run in a worker thread via
        # asyncio.to_thread without tripping SQLite's thread affinity check.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS aircraft (
                icao TEXT PRIMARY KEY,
                registration TEXT,
                aircraft_type TEXT,
                aircraft_model TEXT,
                manufacturer TEXT,
                owner_operator TEXT,
                year_built TEXT,
                is_military INTEGER
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        self._conn.commit()
        self._count: int | None = None

    # ------------------------------------------------------------------
    # Freshness
    # ------------------------------------------------------------------

    def _meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @staticmethod
    def _fingerprint(source: Path) -> str:
        """Identify a snapshot by size and mtime - cheap, and enough to notice a
        re-download without re-hashing 15 MB of gzip."""
        st = source.stat()
        return f"{SCHEMA_VERSION}:{st.st_size}:{int(st.st_mtime)}"

    def is_current_for(self, source: Path) -> bool:
        """True when the index was built from exactly this snapshot."""
        try:
            if self.count == 0:
                return False
            return self._meta("source_fingerprint") == self._fingerprint(source)
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def rebuild(self, rows: Iterable[tuple[str, dict[str, Any]]], source: Path) -> int:
        """Replace the index contents. Blocking - call via asyncio.to_thread.

        `rows` yields ``(icao_upper, normalized_entry)``. Written in one
        transaction so a crash mid-build leaves the previous index intact
        rather than a half-populated one.
        """
        inserted = 0
        with self._conn:  # single transaction
            self._conn.execute("DELETE FROM aircraft")
            stmt = (
                "INSERT OR REPLACE INTO aircraft "
                "(icao, registration, aircraft_type, aircraft_model, manufacturer, "
                " owner_operator, year_built, is_military) "
                "VALUES (?,?,?,?,?,?,?,?)"
            )
            for icao, entry in rows:
                self._conn.execute(
                    stmt,
                    (
                        icao,
                        entry.get("registration"),
                        entry.get("aircraft_type"),
                        entry.get("aircraft_model"),
                        entry.get("manufacturer"),
                        entry.get("owner_operator"),
                        entry.get("year_built"),
                        _bool_to_int(entry.get("is_military")),
                    ),
                )
                inserted += 1
            self._set_meta("source_fingerprint", self._fingerprint(source))
        self._count = inserted
        # The bulk insert leaves a WAL as large as the database itself, and this
        # index is read-only from here on, so nothing would ever checkpoint it.
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            logger.debug("WAL checkpoint after rebuild failed", exc_info=True)
        logger.info("Aircraft index rebuilt: %d entries at %s", inserted, self.path)
        return inserted

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def lookup(self, icao_upper: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT registration, aircraft_type, aircraft_model, manufacturer, "
            "owner_operator, year_built, is_military FROM aircraft WHERE icao = ?",
            (icao_upper,),
        ).fetchone()
        if row is None:
            return None
        result: dict[str, Any] = dict(zip(_COLUMNS, row))
        result["is_military"] = _int_to_bool(result["is_military"])
        return result

    def has(self, icao_upper: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM aircraft WHERE icao = ? LIMIT 1", (icao_upper,)
            ).fetchone()
            is not None
        )

    @property
    def count(self) -> int:
        if self._count is None:
            self._count = self._conn.execute("SELECT COUNT(*) FROM aircraft").fetchone()[0]
        return self._count

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass


def _bool_to_int(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def _int_to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def iter_snapshot(path: Path, normalize) -> Iterator[tuple[str, dict[str, Any]]]:
    """Stream ``(icao_upper, normalized)`` pairs out of the gzipped snapshot.

    Streaming matters: the point of the index is to avoid ever holding all
    616k entries at once, so this must not build a dict on the way through.
    Accepts both a single JSON object keyed by ICAO and the newline-delimited
    form, matching what the loader already tolerated.
    """
    import gzip
    import json

    with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as fh:
        first = fh.read(1)
        while first and first.isspace():
            first = fh.read(1)
        fh.seek(0)

        if first == "{":
            # Could be either one big object or NDJSON of objects. Try NDJSON
            # first since it streams; fall back to a whole-file parse.
            saw_any = False
            for line in fh:
                line = line.strip().rstrip(",")
                if not line or line in ("{", "}"):
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    saw_any = False
                    break
                if not isinstance(entry, dict):
                    continue
                icao = entry.get("icao") or entry.get("hex") or entry.get("ModeS")
                if not icao:
                    continue
                saw_any = True
                yield str(icao).upper(), normalize(entry)
            if saw_any:
                return
            # Whole-file object keyed by ICAO.
            fh.seek(0)
            data = json.load(fh)
            if isinstance(data, dict):
                for key, entry in data.items():
                    if isinstance(entry, dict):
                        yield str(key).upper(), normalize(entry)
                    else:
                        yield str(key).upper(), normalize({"icao": key})
