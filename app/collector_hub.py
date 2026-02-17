"""Collector Hub — TCP server that accepts raw hex ADS-B feeds from collectors.

Listens on a dedicated TCP port. Each collector connects and:
  1. Sends a JSON hello line: {"id":"abc","name":"roof","lat":38.8,"lon":-77.0}
  2. Streams raw hex Mode S messages, one per line.

The hub decodes each message using pyModeS and feeds results into the
AircraftStore, tracking which collectors are connected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.decoder import Decoder
from app.models import CollectorInfo

if TYPE_CHECKING:
    from app.aircraft_store import AircraftStore

logger = logging.getLogger(__name__)


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
        self._rate_timestamps: list[float] = []

    @property
    def aircraft_count(self) -> int:
        return 0  # populated from store tracking

    def record_message(self):
        now = time.time()
        self.messages_total += 1
        self._rate_timestamps.append(now)
        cutoff = now - 10
        self._rate_timestamps = [t for t in self._rate_timestamps if t > cutoff]
        count = len(self._rate_timestamps)
        elapsed = min(10.0, now - self._rate_timestamps[0]) if self._rate_timestamps else 10.0
        self.messages_per_second = round(count / max(elapsed, 0.1), 1)
        self.last_heartbeat = datetime.now()


class CollectorHub:
    """TCP server that accepts raw hex feeds from remote collectors."""

    def __init__(self, store: AircraftStore, port: int = 4002):
        self._store = store
        self._port = port
        self._decoder = Decoder()
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
            while True:
                line_bytes = await reader.readline()
                if not line_bytes:
                    break  # connection closed

                hex_msg = line_bytes.decode("ascii", errors="ignore").strip()
                if not hex_msg:
                    continue

                # Strip * ; framing if present
                if hex_msg.startswith("*") and hex_msg.endswith(";"):
                    hex_msg = hex_msg[1:-1]
                hex_msg = hex_msg.upper()

                if len(hex_msg) not in (14, 28):
                    continue

                conn.record_message()

                decoded = self._decoder.decode(hex_msg, ref_lat, ref_lon)
                if decoded:
                    await self._store.update(decoded, source_collector=collector_id)

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
