from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket

from app.models import Aircraft, ConnectedClientInfo

if TYPE_CHECKING:
    from app.aircraft_db import AircraftDB
    from app.type_collector import TypeCollector

logger = logging.getLogger(__name__)

STALE_TIMEOUT_SECONDS = 60
VERTICAL_RATE_LEVEL_THRESHOLD = 100  # ft/min — below this is considered "level"

# ── Broadcast throttling ────────────────────────────────────────────────────
# High-rate transponders (especially nearby/on-ground aircraft) can generate
# hundreds of decoded messages per second.  Broadcasting every one of them
# saturates the asyncio event loop and the WebSocket link to the browser.
# We enforce a minimum interval between broadcasts for the same ICAO.
BROADCAST_MIN_INTERVAL = 0.15   # seconds — at most ~7 broadcasts/sec per aircraft
STATS_CACHE_TTL = 1.0           # seconds to cache computed stats

# Type alias for update/remove callbacks
UpdateCallback = Callable[[dict], Any]   # receives aircraft dict
RemoveCallback = Callable[[str], Any]    # receives ICAO string


@dataclass
class ConnectedClient:
    """Metadata for a connected WebSocket client."""
    ws: WebSocket
    client_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    client_type: str = "browser"  # "browser" or "api"
    connected_at: datetime = field(default_factory=datetime.now)
    remote_addr: str = ""


class AircraftStore:
    """In-memory store for tracked aircraft. Receives updates from the SBS client,
    maintains current state, prunes stale entries, and notifies WebSocket clients."""

    def __init__(self, aircraft_db: AircraftDB | None = None, type_collector: TypeCollector | None = None):
        self._aircraft: dict[str, Aircraft] = {}
        self._clients: list[ConnectedClient] = []
        self._lock = asyncio.Lock()
        self._prune_task: asyncio.Task | None = None
        self._save_types_task: asyncio.Task | None = None
        self._broadcast_task: asyncio.Task | None = None
        self._messages_total = 0
        self._positions_total = 0
        self._message_timestamps: deque[float] = deque()
        self._position_timestamps: deque[float] = deque()
        self._start_time = time.time()
        self._aircraft_db = aircraft_db
        self._type_collector = type_collector

        # Receiver position for distance/bearing calculations
        self.receiver_lat: float | None = None
        self.receiver_lon: float | None = None

        # Previous state snapshots for rate-of-change calculations (keyed by ICAO)
        self._prev_state: dict[str, dict[str, Any]] = {}

        # Callbacks fired after updates/removes (used by upstream client)
        self._on_update_callbacks: list[UpdateCallback] = []
        self._on_remove_callbacks: list[RemoveCallback] = []

        # Multi-collector tracking: ICAO -> set of collector IDs reporting it
        self._source_collectors: dict[str, set[str]] = {}
        # collector_id -> (lat, lon) for nearest_collector_nm
        self._collector_positions: dict[str, tuple[float, float]] = {}

        # Broadcast throttling — per-ICAO rate limiting
        self._last_broadcast: dict[str, float] = {}
        self._dirty_aircraft: set[str] = set()

        # Stats caching
        self._stats_cache: dict | None = None
        self._stats_cache_time: float = 0

    async def start(self):
        """Start the background prune task and type-save task."""
        self._start_time = time.time()
        self._prune_task = asyncio.create_task(self._prune_loop())
        self._save_types_task = asyncio.create_task(self._save_types_loop())
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())

    async def stop(self):
        """Stop background tasks and save collected types."""
        for task in (self._prune_task, self._save_types_task, self._broadcast_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._type_collector:
            self._type_collector.save()

    def on_update(self, callback: UpdateCallback):
        """Register a callback invoked after every aircraft update.

        The callback receives the aircraft dict (JSON-serialisable).
        If the callback is a coroutine function the result is awaited."""
        self._on_update_callbacks.append(callback)

    def on_remove(self, callback: RemoveCallback):
        """Register a callback invoked when an aircraft is pruned.

        The callback receives the ICAO string."""
        self._on_remove_callbacks.append(callback)

    async def _fire_update_callbacks(self, ac_dict: dict):
        for cb in self._on_update_callbacks:
            try:
                result = cb(ac_dict)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.debug("on_update callback error", exc_info=True)

    async def _fire_remove_callbacks(self, icao: str):
        for cb in self._on_remove_callbacks:
            try:
                result = cb(icao)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.debug("on_remove callback error", exc_info=True)

    async def update(
        self,
        data: dict[str, Any],
        source_collector: str | None = None,
        preprocessed: bool = False,
        observed_at: float | None = None,
    ):
        """Update an aircraft's state with new data.

        Args:
            data: Dict with at least an ``icao`` key plus optional ADS-B fields.
            source_collector: If provided, tags this aircraft with the reporting
                collector ID (used by the central server for multi-source merging).
            observed_at: Unix seconds at which the collector actually received
                the message, when it told us (feed protocol 2). Spooled traffic
                replayed after an uplink outage carries its true observation
                time, so timestamps must come from here rather than from arrival
                time. ``None`` means "time it on arrival", the protocol-1
                behaviour. A sample older than what is already stored for this
                aircraft is applied without rewinding its clocks - see
                ``stale_sample`` below.
            preprocessed: When True the collector already decoded and baked
                attitude (roll/pitch/gamma) at ingest time — not display
                filtering, just a richer decode. Kinematics (lat/lon/track/
                speed/altitude) are never snapped, EMA'd, or otherwise
                filtered anywhere in this pipeline, preprocessed or not.
        """
        icao = str(data.pop("icao")).upper()
        if preprocessed:
            data.setdefault("preprocessed", True)
        has_position = "latitude" in data and "longitude" in data
        # Aircraft state is stamped with the observation time; ingest-rate
        # metrics are stamped with arrival time, so replaying a backlog cannot
        # push out-of-order entries into the rate windows.
        arrival_ts = time.time()
        now_ts = arrival_ts if observed_at is None else observed_at
        now = datetime.fromtimestamp(now_ts)
        is_new = False

        # Live Mode-S truth: do NOT snap altitude to 25 ft, or filter/smooth
        # any kinematics. This server and every downstream client (WOPR)
        # display the feed exactly as received.

        # On-ground aircraft: clamp altitude to 0.  Barometric pressure at low
        # elevations can produce slightly negative Gillham-code altitudes (e.g.
        # -75 ft) that flicker with 0 ft and look wrong in the UI.
        if data.get("on_ground") is True:
            if "altitude" in data and data["altitude"] is not None and data["altitude"] < 0:
                data["altitude"] = 0
            if "alt_geom" in data and data["alt_geom"] is not None and data["alt_geom"] < 0:
                data["alt_geom"] = 0

        async with self._lock:
            if source_collector:
                self._source_collectors.setdefault(icao, set()).add(source_collector)

            if icao in self._aircraft:
                ac = self._aircraft[icao]
                update_fields = {k: v for k, v in data.items() if v is not None}
                update_fields["message_count"] = ac.message_count + 1

                # A replayed sample can arrive after fresher live data for the
                # same aircraft. Its field values are still real observations
                # worth keeping, but its clocks are in the past: advancing
                # last_seen/position_updated backwards would make a live
                # aircraft look stale, and feeding a negative dt into the
                # inferred-field derivation would produce nonsense turn rates.
                stale_sample = observed_at is not None and ac.last_seen is not None and ac.last_seen > now
                if not stale_sample:
                    update_fields["last_seen"] = now
                    # Position clock: advances ONLY with an actual position decode.
                    # last_seen advances on every message (velocity/squawk/altitude),
                    # which makes it useless for downstream position-rate checks.
                    if update_fields.get("latitude") is not None and update_fields.get("longitude") is not None:
                        update_fields["position_updated"] = now

                # Clamp negative altitudes for aircraft known to be on the ground.
                # DF4/16/20 surveillance replies carry only altitude (no on_ground flag),
                # so we re-check against the stored aircraft state here inside the lock.
                effective_on_ground = update_fields.get("on_ground", ac.on_ground)
                if effective_on_ground is True:
                    if update_fields.get("altitude") is not None and update_fields["altitude"] < 0:
                        update_fields["altitude"] = 0
                    if update_fields.get("alt_geom") is not None and update_fields["alt_geom"] < 0:
                        update_fields["alt_geom"] = 0

                if stale_sample:
                    # Derivations assume monotonic time; skip them rather than
                    # corrupt _prev_state with an out-of-order sample.
                    inferred = {}
                elif preprocessed:
                    inferred = self._compute_inferred_preprocessed(
                        icao, ac, update_fields, now_ts
                    )
                else:
                    inferred = self._compute_inferred(icao, ac, update_fields, now_ts)
                update_fields.update(inferred)

                if icao in self._source_collectors:
                    update_fields["source_collectors"] = sorted(self._source_collectors[icao])
                lat_for_nearest = update_fields.get("latitude", ac.latitude)
                lon_for_nearest = update_fields.get("longitude", ac.longitude)
                nearest = self._nearest_collector_nm(icao, lat_for_nearest, lon_for_nearest)
                if nearest is not None:
                    update_fields["nearest_collector_nm"] = nearest

                self._aircraft[icao] = ac.model_copy(update=update_fields)
            else:
                non_none = {k: v for k, v in data.items() if v is not None}
                extra: dict[str, Any] = {}
                if icao in self._source_collectors:
                    extra["source_collectors"] = sorted(self._source_collectors[icao])
                if non_none.get("latitude") is not None and non_none.get("longitude") is not None:
                    extra["position_updated"] = now
                nearest = self._nearest_collector_nm(
                    icao, non_none.get("latitude"), non_none.get("longitude")
                )
                if nearest is not None:
                    extra["nearest_collector_nm"] = nearest
                ac_obj = Aircraft(
                    icao=icao,
                    first_seen=now,
                    last_seen=now,
                    message_count=1,
                    **non_none,
                    **extra,
                )
                inferred = self._compute_inferred_initial(ac_obj)
                if inferred:
                    ac_obj = ac_obj.model_copy(update=inferred)
                self._aircraft[icao] = ac_obj
                is_new = True

                self._prev_state[icao] = {
                    "track": ac_obj.track,
                    "ground_speed": ac_obj.ground_speed,
                    "ts": now_ts,
                }

            self._messages_total += 1
            self._message_timestamps.append(arrival_ts)
            if has_position:
                self._positions_total += 1
                self._position_timestamps.append(arrival_ts)

            # Invalidate stats cache
            self._stats_cache = None

            # Throttle: only serialize + broadcast if enough time has passed
            # for this ICAO (new aircraft always broadcast immediately). No
            # other suppression — every accepted raw update reaches clients
            # verbatim, backward jumps and all (Mode-S truth contract).
            last_ok = self._last_broadcast.get(icao, 0)
            silence = now_ts - last_ok
            should_broadcast = is_new or silence >= BROADCAST_MIN_INTERVAL

            if should_broadcast:
                ac_dict = self._aircraft[icao].model_dump(mode="json")
                stats = self._compute_stats()
                self._last_broadcast[icao] = now_ts
            else:
                self._dirty_aircraft.add(icao)
                ac_dict = None
                stats = None

        if is_new and self._aircraft_db is not None:
            asyncio.create_task(self._enrich_aircraft(icao))

        if ac_dict is None:
            return

        await self._broadcast(ac_dict, stats)
        await self._fire_update_callbacks(ac_dict)

    # ------------------------------------------------------------------
    # Inferred-field computation
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Smoothing helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Inferred-field computation
    # ------------------------------------------------------------------

    def _compute_inferred(
        self, icao: str, ac: Aircraft, update_fields: dict, now_ts: float
    ) -> dict[str, Any]:
        """Compute inferred fields by comparing previous and current state.

        Live Mode-S path: no filtering, no smoothing. Computes turn_rate,
        speed_trend, flight_phase, distance/bearing, and alt_geom enrichment
        purely from the raw values, plus this receiver's own geometry.

        Must be called while holding self._lock.
        """
        inferred: dict[str, Any] = {}
        prev = self._prev_state.get(icao)

        # Current raw values (prefer incoming update, fall back to existing aircraft)
        cur_track = update_fields.get("track", ac.track)
        cur_speed = update_fields.get("ground_speed", ac.ground_speed)
        cur_vr = update_fields.get("vertical_rate", ac.vertical_rate)
        cur_on_ground = update_fields.get("on_ground", ac.on_ground)
        cur_lat = update_fields.get("latitude", ac.latitude)
        cur_lon = update_fields.get("longitude", ac.longitude)

        # ── Rate-of-change fields (need previous state + time delta) ─────
        if prev is not None:
            dt = now_ts - prev["ts"]
            if dt > 0 and dt < STALE_TIMEOUT_SECONDS:
                # Turn rate (degrees/sec, positive = right, handles 360 wrap)
                if cur_track is not None and prev.get("track") is not None:
                    delta_track = (cur_track - prev["track"] + 540) % 360 - 180
                    inferred["turn_rate"] = round(delta_track / dt, 2)

                # Speed trend (knots/sec)
                if cur_speed is not None and prev.get("ground_speed") is not None:
                    delta_speed = cur_speed - prev["ground_speed"]
                    inferred["speed_trend"] = round(delta_speed / dt, 2)

        # Flight phase (instantaneous, no history needed)
        inferred["flight_phase"] = self._classify_flight_phase(cur_vr, cur_on_ground)

        # Distance & bearing from receiver
        dist, brg = self._distance_bearing(cur_lat, cur_lon)
        if dist is not None:
            inferred["distance_nm"] = dist
            inferred["bearing"] = brg

        # ── WGS84 geometric altitude ────────────────────────────────────
        inferred.update(self._compute_alt_geom(ac, update_fields))

        # Update previous state snapshot for next computation
        self._prev_state[icao] = {
            "track": cur_track,
            "ground_speed": cur_speed,
            "ts": now_ts,
        }

        return inferred

    def _compute_inferred_preprocessed(
        self, icao: str, ac: Aircraft, update_fields: dict, now_ts: float
    ) -> dict[str, Any]:
        """Inferred-field path for **preprocessed** collector feeds.

        The collector-side attitude bake is a decode-time enrichment, not
        display filtering — we do NOT re-derive or smooth anything server-side.
        We still compute purely server-side geometry (distance, bearing,
        flight phase) because those depend on this receiver's location.
        Geometric altitude and attitude (``alt_geom``, ``heading``, ``roll_deg``,
        ``pitch_deg``, ``gamma_deg``) arrive baked and are stored as-is.

        Must be called while holding self._lock.
        """
        inferred: dict[str, Any] = {}

        cur_vr = update_fields.get("vertical_rate", ac.vertical_rate)
        cur_on_ground = update_fields.get("on_ground", ac.on_ground)
        cur_lat = update_fields.get("latitude", ac.latitude)
        cur_lon = update_fields.get("longitude", ac.longitude)

        inferred["flight_phase"] = self._classify_flight_phase(cur_vr, cur_on_ground)

        dist, brg = self._distance_bearing(cur_lat, cur_lon)
        if dist is not None:
            inferred["distance_nm"] = dist
            inferred["bearing"] = brg

        self._prev_state[icao] = {
            "track": update_fields.get("track", ac.track),
            "ground_speed": update_fields.get("ground_speed", ac.ground_speed),
            "ts": now_ts,
        }

        return inferred

    @staticmethod
    def _compute_alt_geom(ac: Aircraft, update_fields: dict) -> dict[str, Any]:
        """Derive WGS84 geometric altitude from available data.

        Priority:
          1. ``alt_geom`` supplied directly (TC 20-22 GNSS altitude) — use as-is.
          2. Barometric ``altitude`` + ``alt_diff`` (GNSS–baro offset from TC 19)
             → ``alt_geom = altitude + alt_diff``.
          3. No conversion possible — leave ``alt_geom`` unset.
        """
        result: dict[str, Any] = {}

        # If the incoming update already carries a GNSS geometric altitude
        # (TC 20-22), it was set by the decoder — nothing more to compute.
        if update_fields.get("alt_geom") is not None:
            return result

        # Best available barometric altitude and GNSS–baro difference
        baro = update_fields.get("altitude", ac.altitude)
        alt_diff = update_fields.get("alt_diff", ac.alt_diff)

        if baro is not None and alt_diff is not None:
            result["alt_geom"] = baro + alt_diff

        return result

    def _compute_inferred_initial(self, ac: Aircraft) -> dict[str, Any]:
        """Compute what inferred fields we can from a single (first) message."""
        inferred: dict[str, Any] = {}

        inferred["flight_phase"] = self._classify_flight_phase(ac.vertical_rate, ac.on_ground)

        dist, brg = self._distance_bearing(ac.latitude, ac.longitude)
        if dist is not None:
            inferred["distance_nm"] = dist
            inferred["bearing"] = brg

        # WGS84 geometric altitude (first message — only if both parts are
        # present and the collector didn't already supply a geometric altitude,
        # which preprocessed feeds do).
        if ac.alt_geom is None and ac.altitude is not None and ac.alt_diff is not None:
            inferred["alt_geom"] = ac.altitude + ac.alt_diff

        return inferred

    @staticmethod
    def _classify_flight_phase(
        vertical_rate: int | None, on_ground: bool | None
    ) -> str | None:
        """Classify the flight phase from vertical rate and ground status."""
        if on_ground is True:
            return "on_ground"
        if vertical_rate is None:
            return None
        if vertical_rate > VERTICAL_RATE_LEVEL_THRESHOLD:
            return "climbing"
        if vertical_rate < -VERTICAL_RATE_LEVEL_THRESHOLD:
            return "descending"
        return "level"

    def _distance_bearing(
        self, lat: float | None, lon: float | None
    ) -> tuple[float | None, float | None]:
        """Compute great-circle distance (nm) and bearing from receiver to aircraft."""
        if (
            lat is None
            or lon is None
            or self.receiver_lat is None
            or self.receiver_lon is None
        ):
            return None, None
        return self._distance_bearing_between(
            self.receiver_lat, self.receiver_lon, lat, lon
        )

    async def _enrich_aircraft(self, icao: str):
        """Look up aircraft metadata and merge it into the stored aircraft."""
        try:
            info = await self._aircraft_db.lookup(icao)
            if info is None:
                return
            async with self._lock:
                ac = self._aircraft.get(icao)
                if ac is None:
                    return
                enrichment = {k: v for k, v in info.items() if v is not None}
                self._aircraft[icao] = ac.model_copy(update=enrichment)
                ac_dict = self._aircraft[icao].model_dump(mode="json")
                stats = self._compute_stats()
            # Record the type/model in the collector
            if self._type_collector and info:
                self._type_collector.record(info)
            await self._broadcast(ac_dict, stats)
        except Exception:
            logger.debug("Failed to enrich aircraft %s", icao, exc_info=True)

    def set_collector_position(
        self, collector_id: str, lat: float | None, lon: float | None
    ) -> None:
        """Record a collector's receiver lat/lon for nearest_collector_nm."""
        if lat is None or lon is None:
            self._collector_positions.pop(collector_id, None)
            return
        self._collector_positions[collector_id] = (float(lat), float(lon))

    def _nearest_collector_nm(
        self, icao: str, lat: float | None, lon: float | None
    ) -> float | None:
        """Distance (nm) from the aircraft to the nearest reporting collector."""
        if lat is None or lon is None:
            return None
        sources = self._source_collectors.get(icao)
        if not sources:
            return None
        best: float | None = None
        for cid in sources:
            pos = self._collector_positions.get(cid)
            if pos is None:
                continue
            dist, _ = self._distance_bearing_between(lat, lon, pos[0], pos[1])
            if dist is None:
                continue
            if best is None or dist < best:
                best = dist
        return best

    @staticmethod
    def _distance_bearing_between(
        lat1: float, lon1: float, lat2: float, lon2: float
    ) -> tuple[float | None, float | None]:
        """Haversine nm + initial bearing between two LLA points."""
        r_lat1 = math.radians(lat1)
        r_lon1 = math.radians(lon1)
        r_lat2 = math.radians(lat2)
        r_lon2 = math.radians(lon2)
        dlat = r_lat2 - r_lat1
        dlon = r_lon2 - r_lon1
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(r_lat1) * math.cos(r_lat2) * math.sin(dlon / 2) ** 2
        )
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        EARTH_NM = 3440.065
        distance = round(EARTH_NM * c, 1)
        x = math.sin(dlon) * math.cos(r_lat2)
        y = (
            math.cos(r_lat1) * math.sin(r_lat2)
            - math.sin(r_lat1) * math.cos(r_lat2) * math.cos(dlon)
        )
        bearing = round(math.degrees(math.atan2(x, y)) % 360, 1)
        return distance, bearing

    def remove_collector_source(self, collector_id: str):
        """Remove a collector ID from all aircraft source tracking.

        Called when a collector disconnects from the server. Refreshes the
        materialized ``source_collectors`` / ``nearest_collector_nm`` fields
        immediately so clients do not see a departed collector until prune.
        """
        self._collector_positions.pop(collector_id, None)
        affected: list[str] = []
        for icao, sources in list(self._source_collectors.items()):
            if collector_id not in sources:
                continue
            sources.discard(collector_id)
            if not sources:
                del self._source_collectors[icao]
            affected.append(icao)

        for icao in affected:
            ac = self._aircraft.get(icao)
            if ac is None:
                continue
            srcs = sorted(self._source_collectors.get(icao, set()))
            nearest = self._nearest_collector_nm(icao, ac.latitude, ac.longitude)
            self._aircraft[icao] = ac.model_copy(
                update={
                    "source_collectors": srcs if srcs else None,
                    "nearest_collector_nm": nearest,
                }
            )

    async def get_all(self) -> list[Aircraft]:
        """Return all currently tracked aircraft."""
        async with self._lock:
            return list(self._aircraft.values())

    async def get_by_icao(self, icao: str) -> Aircraft | None:
        """Return a single aircraft by ICAO hex code."""
        async with self._lock:
            return self._aircraft.get(icao.upper())

    def _compute_stats(self) -> dict:
        """Compute stats. Must be called while holding self._lock or from sync context.

        Uses deque-based O(k) trimming (k = expired entries) instead of
        rebuilding the full list, and caches the result for STATS_CACHE_TTL
        to avoid redundant computation on high-rate message streams.
        """
        now = time.time()

        if self._stats_cache is not None and now - self._stats_cache_time < STATS_CACHE_TTL:
            return self._stats_cache

        uptime = now - self._start_time
        cutoff = now - 60

        while self._message_timestamps and self._message_timestamps[0] < cutoff:
            self._message_timestamps.popleft()
        while self._position_timestamps and self._position_timestamps[0] < cutoff:
            self._position_timestamps.popleft()

        window = min(60, uptime) if uptime > 0 else 1
        mps = len(self._message_timestamps) / window
        pps = len(self._position_timestamps) / window

        with_position = sum(
            1 for ac in self._aircraft.values()
            if ac.latitude is not None and ac.longitude is not None
        )

        result = {
            "uptime_seconds": round(uptime, 1),
            "aircraft_count": len(self._aircraft),
            "aircraft_with_position": with_position,
            "messages_total": self._messages_total,
            "messages_per_second": round(mps, 1),
            "positions_total": self._positions_total,
            "positions_per_second": round(pps, 1),
        }
        self._stats_cache = result
        self._stats_cache_time = now
        return result

    def get_stats(self) -> dict:
        """Return receiver statistics (for REST API)."""
        return self._compute_stats()

    async def register_websocket(
        self, ws: WebSocket, client_type: str = "browser", remote_addr: str = ""
    ) -> ConnectedClient:
        """Register a WebSocket client to receive updates."""
        client = ConnectedClient(
            ws=ws, client_type=client_type, remote_addr=remote_addr,
        )
        self._clients.append(client)
        logger.info(
            "WebSocket client connected: %s (%s) from %s (%d total)",
            client.client_id, client_type, remote_addr, len(self._clients),
        )
        return client

    async def unregister_websocket(self, ws: WebSocket):
        """Remove a WebSocket client."""
        self._clients = [c for c in self._clients if c.ws is not ws]
        logger.info("WebSocket client disconnected (%d remaining)", len(self._clients))

    def get_connected_clients(self) -> list[ConnectedClientInfo]:
        """Return metadata about all connected WebSocket clients."""
        return [
            ConnectedClientInfo(
                client_id=c.client_id,
                client_type=c.client_type,
                remote_addr=c.remote_addr,
                connected_since=c.connected_at,
            )
            for c in self._clients
        ]

    async def broadcast_raw(self, data: dict):
        """Send an arbitrary JSON message to all WebSocket clients."""
        if not self._clients:
            return
        message = json.dumps(data)
        disconnected: list[ConnectedClient] = []
        for client in self._clients:
            try:
                await client.ws.send_text(message)
            except Exception:
                disconnected.append(client)
        for client in disconnected:
            if client in self._clients:
                self._clients.remove(client)

    async def _broadcast(self, aircraft_data: dict, stats: dict):
        """Send an aircraft update + live stats to all connected WebSocket clients."""
        if not self._clients:
            return

        message = json.dumps({
            "type": "update",
            "aircraft": aircraft_data,
            "stats": stats,
        })
        disconnected: list[ConnectedClient] = []

        for client in self._clients:
            try:
                await client.ws.send_text(message)
            except Exception:
                disconnected.append(client)

        for client in disconnected:
            if client in self._clients:
                self._clients.remove(client)

    async def _broadcast_loop(self):
        """Periodically flush throttled aircraft updates to WebSocket clients.

        Aircraft that received updates but were throttled by
        BROADCAST_MIN_INTERVAL are accumulated in _dirty_aircraft.
        This loop flushes them every tick so the UI stays current
        without per-message broadcast overhead.
        """
        while True:
            await asyncio.sleep(BROADCAST_MIN_INTERVAL)
            try:
                if not self._dirty_aircraft or not self._clients:
                    continue
                async with self._lock:
                    pending = self._dirty_aircraft.copy()
                    self._dirty_aircraft.clear()
                    now_ts = time.time()
                    stats = self._compute_stats()
                    batch: list[tuple[str, dict]] = []
                    for icao in pending:
                        ac = self._aircraft.get(icao)
                        if ac is None:
                            continue
                        ac_dict = ac.model_dump(mode="json")
                        self._last_broadcast[icao] = now_ts
                        batch.append((icao, ac_dict))
                for icao, ac_dict in batch:
                    await self._broadcast(ac_dict, stats)
                    await self._fire_update_callbacks(ac_dict)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in broadcast loop")

    async def _prune_loop(self):
        """Periodically remove aircraft that haven't been seen recently."""
        while True:
            await asyncio.sleep(10)
            try:
                await self._prune_stale()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error during aircraft pruning")

    async def _save_types_loop(self):
        """Periodically save collected aircraft types to disk."""
        while True:
            await asyncio.sleep(30)
            try:
                if self._type_collector:
                    self._type_collector.save()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error saving aircraft types")

    async def _prune_stale(self):
        """Remove aircraft not seen within STALE_TIMEOUT_SECONDS.

        Emits a `remove` per ICAO so the web UI and WOPR terminate the
        contact; then pushes a fresh stats snapshot so the header count
        doesn't lag behind the prune.
        """
        now = datetime.now()
        removed = []

        async with self._lock:
            stale_icaos = [
                icao for icao, ac in self._aircraft.items()
                if (now - ac.last_seen).total_seconds() > STALE_TIMEOUT_SECONDS
            ]
            for icao in stale_icaos:
                del self._aircraft[icao]
                self._prev_state.pop(icao, None)
                self._source_collectors.pop(icao, None)
                self._last_broadcast.pop(icao, None)
                self._dirty_aircraft.discard(icao)
                removed.append(icao)
            if removed:
                self._stats_cache = None  # counts must reflect the prune
                stats = self._compute_stats()
            else:
                stats = None

        if removed:
            logger.info("Pruned %d stale aircraft: %s", len(removed), ", ".join(removed))
            # Notify WebSocket clients and fire remove callbacks
            for icao in removed:
                message = json.dumps({"type": "remove", "icao": icao})
                disconnected: list[ConnectedClient] = []
                for client in self._clients:
                    try:
                        await client.ws.send_text(message)
                    except Exception:
                        disconnected.append(client)
                for client in disconnected:
                    if client in self._clients:
                        self._clients.remove(client)
                await self._fire_remove_callbacks(icao)
            # Push post-prune stats so the UI count drops immediately even
            # when no other aircraft is updating to carry a stats piggyback.
            if stats is not None and self._clients:
                await self.broadcast_raw({"type": "stats", "stats": stats})
