from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket

from app.models import Aircraft

if TYPE_CHECKING:
    from app.aircraft_db import AircraftDB
    from app.type_collector import TypeCollector

logger = logging.getLogger(__name__)

STALE_TIMEOUT_SECONDS = 60
VERTICAL_RATE_LEVEL_THRESHOLD = 100  # ft/min — below this is considered "level"

# ── Track-smoothing parameters ──────────────────────────────────────────────
# Smoothed inferred fields use an Exponential Moving Average (EMA) to dampen
# jitter caused by stale / out-of-order ADS-B packets.  When the raw value
# deviates significantly from the dead-reckoned prediction, we assume the
# packet is stale and lower the EMA weight so the smoothed value barely moves.
SMOOTHING_ALPHA = 0.7           # EMA weight for normal updates (higher = more responsive)
OUTLIER_ALPHA = 0.15            # EMA weight for suspected stale/outlier packets
TRACK_OUTLIER_DEG = 25.0        # Track deviation from prediction to flag as outlier (°)
SPEED_OUTLIER_KTS = 40.0        # Speed deviation from prediction to flag as outlier (kt)
POSITION_OUTLIER_NM = 1.5       # Position deviation from dead-reckoned prediction (nm)
SMOOTHING_RESET_SECONDS = 30    # Reset smoothing state after a gap this long

# Type alias for update/remove callbacks
UpdateCallback = Callable[[dict], Any]   # receives aircraft dict
RemoveCallback = Callable[[str], Any]    # receives ICAO string


class AircraftStore:
    """In-memory store for tracked aircraft. Receives updates from the SBS client,
    maintains current state, prunes stale entries, and notifies WebSocket clients."""

    def __init__(self, aircraft_db: AircraftDB | None = None, type_collector: TypeCollector | None = None):
        self._aircraft: dict[str, Aircraft] = {}
        self._websockets: list[WebSocket] = []
        self._lock = asyncio.Lock()
        self._prune_task: asyncio.Task | None = None
        self._save_types_task: asyncio.Task | None = None
        self._messages_total = 0
        self._message_timestamps: list[float] = []
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

    async def start(self):
        """Start the background prune task and type-save task."""
        self._start_time = time.time()
        self._prune_task = asyncio.create_task(self._prune_loop())
        self._save_types_task = asyncio.create_task(self._save_types_loop())

    async def stop(self):
        """Stop background tasks and save collected types."""
        if self._prune_task:
            self._prune_task.cancel()
            try:
                await self._prune_task
            except asyncio.CancelledError:
                pass
        if self._save_types_task:
            self._save_types_task.cancel()
            try:
                await self._save_types_task
            except asyncio.CancelledError:
                pass
        # Final save of collected types on shutdown
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

    async def update(self, data: dict[str, Any], source_collector: str | None = None):
        """Update an aircraft's state with new data.

        Args:
            data: Dict with at least an ``icao`` key plus optional ADS-B fields.
            source_collector: If provided, tags this aircraft with the reporting
                collector ID (used by the central server for multi-source merging).
        """
        icao = data.pop("icao")
        now = datetime.now()
        now_ts = time.time()
        is_new = False

        async with self._lock:
            # Track which collector(s) report this ICAO
            if source_collector:
                self._source_collectors.setdefault(icao, set()).add(source_collector)

            if icao in self._aircraft:
                ac = self._aircraft[icao]
                # Merge new fields into existing aircraft
                update_fields = {k: v for k, v in data.items() if v is not None}
                update_fields["last_seen"] = now
                update_fields["message_count"] = ac.message_count + 1

                # Compute inferred fields from previous vs. current state
                inferred = self._compute_inferred(icao, ac, update_fields, now_ts)
                update_fields.update(inferred)

                # Attach multi-collector info when present
                if icao in self._source_collectors:
                    update_fields["source_collectors"] = sorted(self._source_collectors[icao])

                self._aircraft[icao] = ac.model_copy(update=update_fields)
            else:
                non_none = {k: v for k, v in data.items() if v is not None}
                extra: dict[str, Any] = {}
                if icao in self._source_collectors:
                    extra["source_collectors"] = sorted(self._source_collectors[icao])
                ac_obj = Aircraft(
                    icao=icao,
                    first_seen=now,
                    last_seen=now,
                    message_count=1,
                    **non_none,
                    **extra,
                )
                # Compute what we can for the first message (distance, bearing, flight_phase)
                inferred = self._compute_inferred_initial(ac_obj)
                if inferred:
                    ac_obj = ac_obj.model_copy(update=inferred)
                self._aircraft[icao] = ac_obj
                is_new = True

                # Seed previous state for future rate + smoothing calculations
                self._prev_state[icao] = {
                    "track": ac_obj.track,
                    "ground_speed": ac_obj.ground_speed,
                    "ts": now_ts,
                    "s_track": ac_obj.smoothed_track,
                    "s_speed": ac_obj.smoothed_ground_speed,
                    "s_lat": ac_obj.smoothed_latitude,
                    "s_lon": ac_obj.smoothed_longitude,
                }

            self._messages_total += 1
            self._message_timestamps.append(now_ts)

            ac_dict = self._aircraft[icao].model_dump(mode="json")
            stats = self._compute_stats()

        # Enrich new aircraft with metadata from the aircraft database
        if is_new and self._aircraft_db is not None:
            asyncio.create_task(self._enrich_aircraft(icao))

        await self._broadcast(ac_dict, stats)
        await self._fire_update_callbacks(ac_dict)

    # ------------------------------------------------------------------
    # Inferred-field computation
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Smoothing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ema(alpha: float, new_val: float, prev_val: float, *, is_angle: bool = False) -> float:
        """Exponential moving average.  Handles 360° wrapping when *is_angle* is True."""
        if is_angle:
            delta = (new_val - prev_val + 540) % 360 - 180
            return (prev_val + alpha * delta) % 360
        return prev_val + alpha * (new_val - prev_val)

    @staticmethod
    def _dead_reckon(lat: float, lon: float, track_deg: float, speed_kts: float, dt: float) -> tuple[float, float]:
        """Forward-project a position given heading, speed, and elapsed time.

        Uses flat-earth approximation (accurate for the short distances involved).
        Returns (predicted_lat, predicted_lon).
        """
        dist_nm = (speed_kts / 3600) * dt
        track_rad = math.radians(track_deg)
        lat_rad = math.radians(lat)
        dlat = dist_nm * math.cos(track_rad) / 60           # 1° lat ≈ 60 nm
        dlon = dist_nm * math.sin(track_rad) / (60 * max(math.cos(lat_rad), 1e-6))
        return lat + dlat, lon + dlon

    @staticmethod
    def _flat_distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Fast flat-earth distance in nautical miles (good for < ~50 nm)."""
        dlat = (lat2 - lat1) * 60
        dlon = (lon2 - lon1) * 60 * math.cos(math.radians((lat1 + lat2) / 2))
        return math.sqrt(dlat * dlat + dlon * dlon)

    # ------------------------------------------------------------------
    # Inferred-field computation (including smoothing)
    # ------------------------------------------------------------------

    def _compute_inferred(
        self, icao: str, ac: Aircraft, update_fields: dict, now_ts: float
    ) -> dict[str, Any]:
        """Compute inferred fields by comparing previous and current state.

        This also produces *smoothed* variants of track, ground speed, and
        position.  The smoothing uses an adaptive-alpha EMA: when the incoming
        raw value deviates significantly from a dead-reckoned prediction, we
        treat the packet as stale and lower its EMA weight so the smoothed
        output barely moves.

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

        # ── Smoothed fields ──────────────────────────────────────────────
        smoothed = self._compute_smoothed(prev, now_ts, cur_track, cur_speed, cur_lat, cur_lon)
        inferred.update(smoothed)

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
            # Smoothed state carried forward
            "s_track": inferred.get("smoothed_track"),
            "s_speed": inferred.get("smoothed_ground_speed"),
            "s_lat": inferred.get("smoothed_latitude"),
            "s_lon": inferred.get("smoothed_longitude"),
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

    def _compute_smoothed(
        self,
        prev: dict[str, Any] | None,
        now_ts: float,
        cur_track: float | None,
        cur_speed: float | None,
        cur_lat: float | None,
        cur_lon: float | None,
    ) -> dict[str, Any]:
        """Produce smoothed_track, smoothed_ground_speed, smoothed_latitude,
        smoothed_longitude using adaptive-alpha EMA + dead reckoning.

        If there is no usable previous smoothed state (first message, or gap
        too large) the raw values are returned as-is to seed the smoother.
        """
        result: dict[str, Any] = {}

        # First message or gap too large → seed with raw values (no smoothing)
        if prev is None:
            if cur_track is not None:
                result["smoothed_track"] = round(cur_track, 1)
            if cur_speed is not None:
                result["smoothed_ground_speed"] = round(cur_speed, 1)
            if cur_lat is not None and cur_lon is not None:
                result["smoothed_latitude"] = round(cur_lat, 6)
                result["smoothed_longitude"] = round(cur_lon, 6)
            return result

        dt = now_ts - prev["ts"]
        if dt <= 0 or dt >= SMOOTHING_RESET_SECONDS:
            # Gap too long — reset smoother with raw values
            if cur_track is not None:
                result["smoothed_track"] = round(cur_track, 1)
            if cur_speed is not None:
                result["smoothed_ground_speed"] = round(cur_speed, 1)
            if cur_lat is not None and cur_lon is not None:
                result["smoothed_latitude"] = round(cur_lat, 6)
                result["smoothed_longitude"] = round(cur_lon, 6)
            return result

        # Retrieve previous smoothed values (fall back to raw if not yet set)
        s_track_prev = prev.get("s_track") or prev.get("track")
        s_speed_prev = prev.get("s_speed") or prev.get("ground_speed")
        s_lat_prev = prev.get("s_lat")
        s_lon_prev = prev.get("s_lon")

        # ── Smooth track ─────────────────────────────────────────────────
        if cur_track is not None and s_track_prev is not None:
            # Predict track from previous smoothed track + observed turn trend
            prev_turn = prev.get("turn_rate") if prev.get("turn_rate") is not None else 0
            predicted_track = (s_track_prev + prev_turn * dt) % 360
            deviation = abs(((cur_track - predicted_track + 540) % 360) - 180)
            alpha = OUTLIER_ALPHA if deviation > TRACK_OUTLIER_DEG else SMOOTHING_ALPHA
            result["smoothed_track"] = round(self._ema(alpha, cur_track, s_track_prev, is_angle=True), 1)
        elif cur_track is not None:
            result["smoothed_track"] = round(cur_track, 1)

        # ── Smooth ground speed ──────────────────────────────────────────
        if cur_speed is not None and s_speed_prev is not None:
            prev_trend = prev.get("speed_trend") if prev.get("speed_trend") is not None else 0
            predicted_speed = s_speed_prev + prev_trend * dt
            deviation = abs(cur_speed - predicted_speed)
            alpha = OUTLIER_ALPHA if deviation > SPEED_OUTLIER_KTS else SMOOTHING_ALPHA
            result["smoothed_ground_speed"] = round(self._ema(alpha, cur_speed, s_speed_prev), 1)
        elif cur_speed is not None:
            result["smoothed_ground_speed"] = round(cur_speed, 1)

        # ── Smooth position (dead-reckoning blend) ───────────────────────
        if cur_lat is not None and cur_lon is not None and s_lat_prev is not None and s_lon_prev is not None:
            # Dead-reckon from previous smoothed position using smoothed heading/speed
            dr_track = result.get("smoothed_track") or s_track_prev
            dr_speed = result.get("smoothed_ground_speed") or s_speed_prev
            if dr_track is not None and dr_speed is not None:
                pred_lat, pred_lon = self._dead_reckon(s_lat_prev, s_lon_prev, dr_track, dr_speed, dt)
                deviation_nm = self._flat_distance_nm(cur_lat, cur_lon, pred_lat, pred_lon)
                alpha = OUTLIER_ALPHA if deviation_nm > POSITION_OUTLIER_NM else SMOOTHING_ALPHA
            else:
                alpha = SMOOTHING_ALPHA
            result["smoothed_latitude"] = round(self._ema(alpha, cur_lat, s_lat_prev), 6)
            result["smoothed_longitude"] = round(self._ema(alpha, cur_lon, s_lon_prev), 6)
        elif cur_lat is not None and cur_lon is not None:
            result["smoothed_latitude"] = round(cur_lat, 6)
            result["smoothed_longitude"] = round(cur_lon, 6)

        return result

    def _compute_inferred_initial(self, ac: Aircraft) -> dict[str, Any]:
        """Compute what inferred fields we can from a single (first) message."""
        inferred: dict[str, Any] = {}

        inferred["flight_phase"] = self._classify_flight_phase(ac.vertical_rate, ac.on_ground)

        dist, brg = self._distance_bearing(ac.latitude, ac.longitude)
        if dist is not None:
            inferred["distance_nm"] = dist
            inferred["bearing"] = brg

        # Seed smoothed fields with raw values (no history to smooth against yet)
        if ac.track is not None:
            inferred["smoothed_track"] = round(ac.track, 1)
        if ac.ground_speed is not None:
            inferred["smoothed_ground_speed"] = round(ac.ground_speed, 1)
        if ac.latitude is not None and ac.longitude is not None:
            inferred["smoothed_latitude"] = round(ac.latitude, 6)
            inferred["smoothed_longitude"] = round(ac.longitude, 6)

        # WGS84 geometric altitude (first message — only if both parts are present)
        if ac.altitude is not None and ac.alt_diff is not None:
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

        r_lat = math.radians(self.receiver_lat)
        r_lon = math.radians(self.receiver_lon)
        a_lat = math.radians(lat)
        a_lon = math.radians(lon)
        dlat = a_lat - r_lat
        dlon = a_lon - r_lon

        # Haversine distance
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(r_lat) * math.cos(a_lat) * math.sin(dlon / 2) ** 2
        )
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        EARTH_NM = 3440.065  # Earth radius in nautical miles
        distance = round(EARTH_NM * c, 1)

        # Initial bearing (forward azimuth)
        x = math.sin(dlon) * math.cos(a_lat)
        y = math.cos(r_lat) * math.sin(a_lat) - math.sin(r_lat) * math.cos(a_lat) * math.cos(dlon)
        bearing = round(math.degrees(math.atan2(x, y)) % 360, 1)

        return distance, bearing

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

    def remove_collector_source(self, collector_id: str):
        """Remove a collector ID from all aircraft source tracking.

        Called when a collector disconnects from the server."""
        for icao, sources in list(self._source_collectors.items()):
            sources.discard(collector_id)
            if not sources:
                del self._source_collectors[icao]

    async def get_all(self) -> list[Aircraft]:
        """Return all currently tracked aircraft."""
        async with self._lock:
            return list(self._aircraft.values())

    async def get_by_icao(self, icao: str) -> Aircraft | None:
        """Return a single aircraft by ICAO hex code."""
        async with self._lock:
            return self._aircraft.get(icao.upper())

    def _compute_stats(self) -> dict:
        """Compute stats. Must be called while holding self._lock or from sync context."""
        now = time.time()
        uptime = now - self._start_time

        cutoff = now - 60
        self._message_timestamps = [t for t in self._message_timestamps if t > cutoff]
        recent_count = len(self._message_timestamps)
        mps = recent_count / min(60, uptime) if uptime > 0 else 0

        with_position = sum(
            1 for ac in self._aircraft.values()
            if ac.latitude is not None and ac.longitude is not None
        )

        return {
            "uptime_seconds": round(uptime, 1),
            "aircraft_count": len(self._aircraft),
            "aircraft_with_position": with_position,
            "messages_total": self._messages_total,
            "messages_per_second": round(mps, 1),
        }

    def get_stats(self) -> dict:
        """Return receiver statistics (for REST API)."""
        return self._compute_stats()

    async def register_websocket(self, ws: WebSocket):
        """Register a WebSocket client to receive updates."""
        self._websockets.append(ws)
        logger.info("WebSocket client connected (%d total)", len(self._websockets))

    async def unregister_websocket(self, ws: WebSocket):
        """Remove a WebSocket client."""
        if ws in self._websockets:
            self._websockets.remove(ws)
        logger.info("WebSocket client disconnected (%d remaining)", len(self._websockets))

    async def broadcast_raw(self, data: dict):
        """Send an arbitrary JSON message to all WebSocket clients."""
        if not self._websockets:
            return
        message = json.dumps(data)
        disconnected = []
        for ws in self._websockets:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            if ws in self._websockets:
                self._websockets.remove(ws)

    async def _broadcast(self, aircraft_data: dict, stats: dict):
        """Send an aircraft update + live stats to all connected WebSocket clients."""
        if not self._websockets:
            return

        message = json.dumps({
            "type": "update",
            "aircraft": aircraft_data,
            "stats": stats,
        })
        disconnected = []

        for ws in self._websockets:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)

        for ws in disconnected:
            if ws in self._websockets:
                self._websockets.remove(ws)

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
        """Remove aircraft not seen within STALE_TIMEOUT_SECONDS."""
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
                removed.append(icao)

        if removed:
            logger.info("Pruned %d stale aircraft: %s", len(removed), ", ".join(removed))
            # Notify WebSocket clients and fire remove callbacks
            for icao in removed:
                message = json.dumps({"type": "remove", "icao": icao})
                disconnected = []
                for ws in self._websockets:
                    try:
                        await ws.send_text(message)
                    except Exception:
                        disconnected.append(ws)
                for ws in disconnected:
                    if ws in self._websockets:
                        self._websockets.remove(ws)
                await self._fire_remove_callbacks(icao)
