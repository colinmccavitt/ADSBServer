"""Collector Hub — TCP server that accepts raw hex ADS-B feeds from collectors.

Listens on a dedicated TCP port. Each collector connects and:
  1. Sends a JSON hello line: {"id":"abc","name":"roof","lat":38.8,"lon":-77.0,"api_key":"..."}
  2. Receives a JSON ack line: {"ok":true,"proto":2}
  3. Streams Mode S messages, one per line.

Two feed formats are accepted on the same port:

  protocol 1  ``8D4840D6202CC371C32CE0576098``      (bare hex, timed on arrival)
  protocol 2  ``1767225600123,8D4840D6202CC371...`` (millisecond observation time)

Protocol 2 exists so a roaming collector can spool messages to disk while its
uplink is down and replay them afterwards without the hub back-dating them to
the moment they happened to arrive. A comma is an unambiguous discriminator:
raw Mode-S hex never contains one. Collectors that predate the ack keep working
unchanged - they only ever looked for an ``error`` key in the reply.

The hub decodes each message using pyModeS and feeds results into the
AircraftStore, tracking which collectors are connected.
Collectors must supply a valid API key in the hello message (when keys are configured).
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
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
#
# Raised 25 -> 50 after measuring a live feed (239 frames/s, 644 ICAOs) and
# replaying it through this limiter. Distinct messages cluster inside the
# window even when an aircraft's per-second total is modest, so 25/s was
# discarding frames that carried new information - not just duplicate
# receptions the store would have overwritten identically:
#
#   cap   stored  dropped_dup  dropped_DISTINCT  information lost
#    25     5266         1194               721            10.04%
#    50     6257          593               331             4.61%
#   100     6257          593               331             4.61%
#
# The curve is flat above 50, so a higher cap only spends CPU re-storing
# duplicates. Cost of the change is store-side only: WebSocket fan-out is
# throttled independently by BROADCAST_MIN_INTERVAL in aircraft_store.
MAX_UPDATES_PER_ICAO_PER_SEC = 50

# Highest feed protocol this hub understands; advertised in the hello ack.
FEED_PROTO = 2

# Bounds for a collector-supplied observation timestamp. A spooled backlog can
# legitimately be hours old, but a wildly wrong clock (an unsynced Pi that
# booted at the epoch) must not be trusted, or it would back-date live traffic
# and make aircraft look permanently stale.
MAX_TIMESTAMP_AGE_SEC = 7 * 24 * 3600
MAX_TIMESTAMP_SKEW_SEC = 300

# Idle/keepalive tuning. Without these the hub never learns that a collector
# vanished mid-connection: the socket sits in ESTABLISHED forever, leaking an
# fd and a stale entry in /api/collectors. Seen in the wild with 10+ sockets
# left over from a collector that had physically moved continents.
KEEPALIVE_IDLE_SEC = 60
KEEPALIVE_INTVL_SEC = 15
KEEPALIVE_COUNT = 4


def _enable_tcp_keepalive(writer: asyncio.StreamWriter) -> None:
    """Ask the kernel to probe idle collector connections and drop dead ones."""
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for opt, value in (
            ("TCP_KEEPIDLE", KEEPALIVE_IDLE_SEC),
            ("TCP_KEEPINTVL", KEEPALIVE_INTVL_SEC),
            ("TCP_KEEPCNT", KEEPALIVE_COUNT),
        ):
            # TCP_KEEPIDLE/INTVL/CNT are Linux-only; skip whatever is missing
            # rather than losing SO_KEEPALIVE entirely on other platforms.
            if hasattr(socket, opt):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), value)
    except OSError:
        logger.debug("Could not set TCP keepalive on collector socket", exc_info=True)


def parse_feed_line(line: str, now: float) -> tuple[str, float | None]:
    """Split a feed line into ``(hex, observed_unix_seconds_or_None)``.

    Protocol 2 collectors prefix each message with the millisecond wall-clock
    time at which they received it (``1767225600123,8D4840D6...``) so a spooled
    backlog replays with true observation times instead of arrival times. Bare
    protocol-1 hex has no prefix and yields ``None``, meaning "time it on
    arrival". A comma is a safe discriminator: raw Mode-S hex never contains one.

    Timestamps outside :data:`MAX_TIMESTAMP_AGE_SEC` in the past or
    :data:`MAX_TIMESTAMP_SKEW_SEC` in the future are discarded (``None``) so a
    misconfigured collector clock degrades to protocol-1 behaviour instead of
    corrupting the store.
    """
    ts_part, sep, hex_part = line.partition(",")
    if not sep:
        return line, None
    try:
        observed = int(ts_part) / 1000.0
    except ValueError:
        # Not a timestamp prefix after all; treat the whole line as the message
        # so the caller's length check rejects it.
        return line, None
    if observed > now + MAX_TIMESTAMP_SKEW_SEC or observed < now - MAX_TIMESTAMP_AGE_SEC:
        return hex_part, None
    return hex_part, observed


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

    # ------------------------------------------------------------------
    # Pushed (structured JSON) collectors — registered by the /ingest
    # WebSocket endpoint. They stream already-decoded aircraft snapshots
    # instead of raw hex, but share the same connection accounting so the
    # admin dashboard and /api/collectors treat them uniformly.
    # ------------------------------------------------------------------

    async def register_pushed_collector(self, hello: dict[str, Any]) -> _CollectorConnection:
        """Register a structured-push collector and return its connection state."""
        collector_id = hello.get("id", "pushed")
        conn = _CollectorConnection(collector_id, hello)
        async with self._lock:
            self._collectors[collector_id] = conn
        self._store.set_collector_position(
            collector_id, conn.latitude, conn.longitude
        )
        logger.info(
            "Pushed collector connected: %s (%s) [%s, %s]",
            collector_id, conn.name or "unnamed", conn.latitude, conn.longitude,
        )
        return conn

    async def unregister_pushed_collector(self, collector_id: str) -> None:
        """Remove a structured-push collector on disconnect."""
        async with self._lock:
            self._collectors.pop(collector_id, None)
        self._store.remove_collector_source(collector_id)
        logger.info("Pushed collector %s cleaned up", collector_id)

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
        conn: _CollectorConnection | None = None
        _enable_tcp_keepalive(writer)

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

            # Acknowledge the hello and advertise the feed protocol. Collectors
            # written before protocol 2 only inspect this reply for an "error"
            # key, so it is safe for them; it also spares new collectors the
            # 5s read timeout they used to sit through when the hub said
            # nothing at all on success.
            writer.write(json.dumps({"ok": True, "proto": FEED_PROTO}).encode() + b"\n")
            await writer.drain()

            conn = _CollectorConnection(collector_id, hello)

            ref_lat = conn.latitude or 0.0
            ref_lon = conn.longitude or 0.0
            if conn.latitude is None or conn.longitude is None:
                logger.warning(
                    "Collector %s omitted lat/lon in hello — CPR reference "
                    "defaults to (0,0); position decodes will likely fail "
                    "until another collector seeds the aircraft",
                    collector_id,
                )

            async with self._lock:
                self._collectors[collector_id] = conn
            self._store.set_collector_position(
                collector_id, conn.latitude, conn.longitude
            )

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
            _replayed_count = 0
            _icaos_seen: set[str] = set()
            _last_diag = time.time()
            _diag_interval = 30

            _update_interval = 1.0 / MAX_UPDATES_PER_ICAO_PER_SEC
            _last_store_update: dict[str, float] = {}

            while True:
                line_bytes = await reader.readline()
                if not line_bytes:
                    break  # connection closed

                line = line_bytes.decode("ascii", errors="ignore").strip()
                if not line:
                    continue

                _msg_count += 1

                arrival = time.time()
                hex_msg, observed = parse_feed_line(line, arrival)
                if observed is not None:
                    _replayed_count += 1

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

                    # Rate-limit on the message's own clock. Using wall time
                    # here would throw away almost an entire spooled backlog,
                    # because a replay delivers many minutes of traffic inside
                    # a second or two of arrival time.
                    clock = observed if observed is not None else arrival
                    if clock - _last_store_update.get(icao_key, 0) >= _update_interval:
                        _last_store_update[icao_key] = clock
                        await self._store.update(
                            decoded,
                            source_collector=collector_id,
                            observed_at=observed,
                        )
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
                        "%d rate-limited, %d stored, %d unique ICAOs, "
                        "%d timestamped/replayed",
                        collector_id,
                        _msg_count, conn.messages_per_second,
                        _decoded_count, _position_count,
                        _decode_fail_count, _filtered_count,
                        _rate_limited_count, _store_count,
                        len(_icaos_seen), _replayed_count,
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
                # Only tear down registration if it is still *ours*. A collector
                # that reconnects (every WiFi handoff, for a roaming one) has
                # already replaced this entry with its new connection, and a
                # late-dying predecessor must not delete the live one - that
                # silently drops a working feed off /api/collectors and unbinds
                # its aircraft. Zombie sockets made this routine before
                # keepalive was enabled.
                async with self._lock:
                    current = self._collectors.get(collector_id)
                    superseded = current is not None and conn is not None and current is not conn
                    if not superseded:
                        self._collectors.pop(collector_id, None)
                if superseded:
                    logger.info(
                        "Collector %s stale connection closed; live connection kept",
                        collector_id,
                    )
                else:
                    self._store.remove_collector_source(collector_id)
                    logger.info("Collector %s cleaned up", collector_id)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
