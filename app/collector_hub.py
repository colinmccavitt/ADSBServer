"""Collector Hub — TCP server that accepts raw hex ADS-B feeds from collectors.

Listens on a dedicated TCP port. Each collector connects and:
  1. Sends a JSON hello line: {"id":"abc","name":"roof","lat":38.8,"lon":-77.0,"api_key":"..."}
  2. Streams raw hex Mode S messages, one per line.

The hub decodes each message using pyModeS and feeds results into the
AircraftStore, tracking which collectors are connected.
Collectors must supply a valid API key in the hello message (when keys are configured).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app import auth
from app.decoder import Decoder
from app.models import CollectorInfo

if TYPE_CHECKING:
    from app.aircraft_store import AircraftStore

logger = logging.getLogger(__name__)

# A single ADS-B transponder emits ~6-7 messages/sec.  Close-range receivers
# can see 50-200× more due to multipath / duplicate SDR outputs.
# Cap per-ICAO store updates to prevent one loud aircraft from saturating the
# async event loop (the decoded data is largely redundant past this rate).
MAX_UPDATES_PER_ICAO_PER_SEC = 25


class _CollectorConnection:
    """State for a single connected collector."""

    def __init__(self, collector_id: str, info: dict[str, Any]):
        self.collector_id = collector_id
        self.name: str | None = info.get("name")
        self.latitude: float | None = info.get("lat")
        self.longitude: float | None = info.get("lon")
        self.connected_since = datetime.now()
        self.messages_total = 0
        self.messages_per_second = 0.0
        self.last_heartbeat = datetime.now()
        self._rate_count = 0
        self._rate_window_start = time.time()

    @property
    def aircraft_count(self) -> int:
        return 0  # populated from store tracking

    def record_message(self):
        self.messages_total += 1
        self._rate_count += 1
        now = time.time()
        elapsed = now - self._rate_window_start
        if elapsed >= 2.0:
            self.messages_per_second = round(self._rate_count / elapsed, 1)
            self._rate_count = 0
            self._rate_window_start = now
            self.last_heartbeat = datetime.now()


class CollectorHub:
    """TCP server that accepts raw hex feeds from remote collectors."""

    def __init__(self, store: AircraftStore, port: int = 4002):
        self._store = store
        self._port = port
        db_check = store._aircraft_db.has_icao if store._aircraft_db else None
        self._decoder = Decoder(db_icao_check=db_check)
        self._collectors: dict[str, _CollectorConnection] = {}
        self._lock = asyncio.Lock()
        self._server: asyncio.Server | None = None

    async def start(self):
        """Start the TCP server listening for collector connections."""
        self._server = await asyncio.start_server(
            self._handle_connection, "0.0.0.0", self._port
        )
        logger.info("Collector hub listening on port %d", self._port)

    async def stop(self):
        """Stop the TCP server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Collector hub stopped")

    def get_collectors(self) -> list[CollectorInfo]:
        """Return info about all currently connected collectors."""
        result = []
        for conn in self._collectors.values():
            # Count aircraft attributed to this collector
            ac_count = sum(
                1 for sources in self._store._source_collectors.values()
                if conn.collector_id in sources
            )
            result.append(CollectorInfo(
                collector_id=conn.collector_id,
                name=conn.name,
                latitude=conn.latitude,
                longitude=conn.longitude,
                connected_since=conn.connected_since,
                aircraft_count=ac_count,
                messages_per_second=conn.messages_per_second,
                last_heartbeat=conn.last_heartbeat,
            ))
        return result

    def get_collector(self, collector_id: str) -> CollectorInfo | None:
        """Return info about a specific collector, or None if not connected."""
        conn = self._collectors.get(collector_id)
        if conn is None:
            return None
        ac_count = sum(
            1 for sources in self._store._source_collectors.values()
            if conn.collector_id in sources
        )
        return CollectorInfo(
            collector_id=conn.collector_id,
            name=conn.name,
            latitude=conn.latitude,
            longitude=conn.longitude,
            connected_since=conn.connected_since,
            aircraft_count=ac_count,
            messages_per_second=conn.messages_per_second,
            last_heartbeat=conn.last_heartbeat,
        )

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        """Handle a single collector TCP connection."""
        addr = writer.get_extra_info("peername")
        collector_id: str | None = None

        try:
            # Read hello line (JSON with collector metadata)
            hello_bytes = await asyncio.wait_for(reader.readline(), timeout=10)
            if not hello_bytes:
                logger.warning("Collector from %s sent empty hello", addr)
                writer.close()
                return

            hello_line = hello_bytes.decode("utf-8", errors="ignore").strip()
            hello = json.loads(hello_line)

            collector_id = hello.get("id", f"unknown-{addr}")

            # Validate API key
            api_key = hello.get("api_key")
            if not auth.validate_collector_key(api_key):
                logger.warning(
                    "Collector %s rejected: invalid API key from %s",
                    collector_id, addr,
                )
                writer.write(b'{"error":"invalid_api_key"}\n')
                await writer.drain()
                writer.close()
                return

            conn = _CollectorConnection(collector_id, hello)

            ref_lat = conn.latitude or 0.0
            ref_lon = conn.longitude or 0.0

            async with self._lock:
                self._collectors[collector_id] = conn

            logger.info(
                "Collector connected: %s (%s) at [%.4f, %.4f] from %s",
                collector_id, conn.name or "unnamed",
                ref_lat, ref_lon, addr,
            )

            # Read raw hex messages, one per line
            _msg_count = 0
            _filtered_count = 0
            _decoded_count = 0
            _decode_fail_count = 0
            _store_count = 0
            _rate_limited_count = 0
            _position_count = 0
            _icaos_seen: set[str] = set()
            _last_diag = time.time()
            _diag_interval = 30

            _update_interval = 1.0 / MAX_UPDATES_PER_ICAO_PER_SEC
            _last_store_update: dict[str, float] = {}

            while True:
                line_bytes = await reader.readline()
                if not line_bytes:
                    break  # connection closed

                hex_msg = line_bytes.decode("ascii", errors="ignore").strip()
                if not hex_msg:
                    continue

                _msg_count += 1

                if hex_msg.startswith("*") and hex_msg.endswith(";"):
                    hex_msg = hex_msg[1:-1]
                hex_msg = hex_msg.upper()

                if len(hex_msg) not in (14, 28):
                    _filtered_count += 1
                    continue

                conn.record_message()

                decoded = self._decoder.decode(hex_msg, ref_lat, ref_lon)
                if decoded:
                    _decoded_count += 1
                    icao_key = decoded.get("icao", "")
                    if "latitude" in decoded and "longitude" in decoded:
                        _position_count += 1
                    _icaos_seen.add(icao_key)

                    now = time.time()
                    if now - _last_store_update.get(icao_key, 0) >= _update_interval:
                        _last_store_update[icao_key] = now
                        await self._store.update(decoded, source_collector=collector_id)
                        _store_count += 1
                    else:
                        _rate_limited_count += 1
                else:
                    _decode_fail_count += 1

                now = time.time()
                if now - _last_diag >= _diag_interval:
                    logger.info(
                        "Collector %s diagnostics — "
                        "%d msgs (%.0f/s), %d decoded, %d with position, "
                        "%d failed CRC/decode, %d filtered (bad length), "
                        "%d rate-limited, %d stored, %d unique ICAOs",
                        collector_id,
                        _msg_count, conn.messages_per_second,
                        _decoded_count, _position_count,
                        _decode_fail_count, _filtered_count,
                        _rate_limited_count, _store_count,
                        len(_icaos_seen),
                    )
                    _last_diag = now

        except asyncio.TimeoutError:
            logger.warning("Collector hello timeout from %s", addr)
        except json.JSONDecodeError:
            logger.warning("Invalid hello JSON from %s", addr)
        except (ConnectionResetError, BrokenPipeError):
            logger.info("Collector disconnected: %s", collector_id or addr)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error in collector connection %s", collector_id or addr)
        finally:
            if collector_id:
                async with self._lock:
                    self._collectors.pop(collector_id, None)
                self._store.remove_collector_source(collector_id)
                logger.info("Collector %s cleaned up", collector_id)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
