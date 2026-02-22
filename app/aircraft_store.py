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

# ── Track-smoothing parameters ──────────────────────────────────────────────
# Smoothed inferred fields use an Exponential Moving Average (EMA) to dampen
# jitter caused by stale / out-of-order ADS-B packets.  When the raw value
# deviates significantly from the dead-reckoned prediction, we assume the
# packet is stale and lower the EMA weight so the smoothed value barely moves.
SMOOTHING_ALPHA = 0.7           # EMA weight for normal updates (higher = more responsive)
OUTLIER_ALPHA = 0.15            # EMA weight for suspected stale/outlier packets
GROUND_STATIONARY_ALPHA = 0.05  # Very low EMA weight for stopped on-ground aircraft
GROUND_STATIONARY_KTS = 5.0     # Speed below which a ground aircraft is considered stationary
TRACK_OUTLIER_DEG = 25.0        # Track deviation from prediction to flag as outlier (°)
SPEED_OUTLIER_KTS = 40.0        # Speed deviation from prediction to flag as outlier (kt)
POSITION_OUTLIER_NM = 1.5       # Position deviation from dead-reckoned prediction (nm)
SMOOTHING_RESET_SECONDS = 30    # Reset smoothing state after a gap this long

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

        # Most-forward broadcast position per ICAO (prevents backward jumps)
        self._broadcast_high_water: dict[str, dict[str, float | None]] = {}

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

    async def update(self, data: dict[str, Any], source_collector: str | None = None):
        """Update an aircraft's state with new data.

        Args:
            data: Dict with at least an ``icao`` key plus optional ADS-B fields.
            source_collector: If provided, tags this aircraft with the reporting
                collector ID (used by the central server for multi-source merging).
        """
        icao = data.pop("icao")
        has_position = "latitude" in data and "longitude" in data
        now = datetime.now()
        now_ts = time.time()
        is_new = False

        # Snap altitude to nearest 25 ft — ADS-B quantization is 25 ft anyway,
        # and raw decoded values oscillate between brackets producing visual noise.
        if "altitude" in data and data["altitude"] is not None:
            data["altitude"] = round(data["altitude"] / 25) * 25
        if "alt_geom" in data and data["alt_geom"] is not None:
            data["alt_geom"] = round(data["alt_geom"] / 25) * 25

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
                update_fields["last_seen"] = now
                update_fields["message_count"] = ac.message_count + 1

                # Clamp negative altitudes for aircraft known to be on the ground.
                # DF4/16/20 surveillance replies carry only altitude (no on_ground flag),
                # so we re-check against the stored aircraft state here inside the lock.
                effective_on_ground = update_fields.get("on_ground", ac.on_ground)
                if effective_on_ground is True:
                    if update_fields.get("altitude") is not None and update_fields["altitude"] < 0:
                        update_fields["altitude"] = 0
                    if update_fields.get("alt_geom") is not None and update_fields["alt_geom"] < 0:
                        update_fields["alt_geom"] = 0

                inferred = self._compute_inferred(icao, ac, update_fields, now_ts)
                update_fields.update(inferred)

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
                inferred = self._compute_inferred_initial(ac_obj)
                if inferred:
                    ac_obj = ac_obj.model_copy(update=inferred)
                self._aircraft[icao] = ac_obj
                is_new = True

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
            if has_position:
                self._positions_total += 1
                self._position_timestamps.append(now_ts)

            # Invalidate stats cache
            self._stats_cache = None

            # Throttle: only serialize + broadcast if enough time has passed
            # for this ICAO (new aircraft always broadcast immediately)
            should_broadcast = is_new or (now_ts - self._last_broadcast.get(icao, 0)) >= BROADCAST_MIN_INTERVAL

            if should_broadcast:
                ac_dict = self._aircraft[icao].model_dump(mode="json")
                stats = self._compute_stats()
                self._last_broadcast[icao] = now_ts

                # Check _is_position_behind inside the lock so we can also
                # reset _prev_state when suppressing.  If the smoothed position
                # retreated behind the high-water mark (stale / outlier packet)
                # we suppress the broadcast AND anchor _prev_state back to the
                # high-water mark so the EMA never starts from a retreated
                # position — that cycle is what causes the visible bouncing.
                if not is_new and self._is_position_behind(icao, ac_dict):
                    hw = self._broadcast_high_water.get(icao)
                    if hw and icao in self._prev_state:
                        self._prev_state[icao]["s_lat"] = hw.get("lat")
                        self._prev_state[icao]["s_lon"] = hw.get("lon")
                    ac_dict = None
                    stats = None
                else:
                    self._update_broadcast_high_water(icao, ac_dict)
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
    # Broadcast high-water mark (prevents backward position jumps)
    # ------------------------------------------------------------------

    def _is_position_behind(self, icao: str, ac_dict: dict) -> bool:
        """Return True if the smoothed position in *ac_dict* is behind the
        most-forward broadcast position for this aircraft.

        "Behind" is measured as a negative displacement along the track
        direction of the high-water mark.  This suppresses stale or
        out-of-order packets that would visually drag the aircraft backward.
        """
        hw = self._broadcast_high_water.get(icao)
        if hw is None:
            return False

        new_lat = ac_dict.get("smoothed_latitude") or ac_dict.get("latitude")
        new_lon = ac_dict.get("smoothed_longitude") or ac_dict.get("longitude")
        if new_lat is None or new_lon is None:
            return False

        hw_lat = hw["lat"]
        hw_lon = hw["lon"]
        hw_track = hw["track"]
        if hw_lat is None or hw_lon is None or hw_track is None:
            return False

        dlat = (new_lat - hw_lat) * 60  # nm (1° lat ≈ 60 nm)
        dlon = (new_lon - hw_lon) * 60 * math.cos(math.radians((hw_lat + new_lat) / 2))

        if dlat * dlat + dlon * dlon < 1e-6:
            return False

        track_rad = math.radians(hw_track)
        forward = dlat * math.cos(track_rad) + dlon * math.sin(track_rad)
        return forward < 0

    def _update_broadcast_high_water(self, icao: str, ac_dict: dict):
        """Record the most-forward broadcast position for *icao*."""
        lat = ac_dict.get("smoothed_latitude") or ac_dict.get("latitude")
        lon = ac_dict.get("smoothed_longitude") or ac_dict.get("longitude")
        track = ac_dict.get("smoothed_track") or ac_dict.get("track")
        if lat is not None and lon is not None:
            self._broadcast_high_water[icao] = {
                "lat": lat, "lon": lon, "track": track,
            }

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
        smoothed = self._compute_smoothed(
            prev, now_ts, cur_track, cur_speed, cur_lat, cur_lon,
            on_ground=cur_on_ground,
        )
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
        on_ground: bool | None = None,
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
                result["smoothed_latitude"] = round(cur_lat, 4)
                result["smoothed_longitude"] = round(cur_lon, 4)
            return result

        dt = now_ts - prev["ts"]
        if dt <= 0 or dt >= SMOOTHING_RESET_SECONDS:
            # Gap too long — reset smoother with raw values
            if cur_track is not None:
                result["smoothed_track"] = round(cur_track, 1)
            if cur_speed is not None:
                result["smoothed_ground_speed"] = round(cur_speed, 1)
            if cur_lat is not None and cur_lon is not None:
                result["smoothed_latitude"] = round(cur_lat, 4)
                result["smoothed_longitude"] = round(cur_lon, 4)
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
            # Stationary ground aircraft: use a very low alpha to heavily damp
            # CPR surface-decoding noise from stopped transponders.
            is_stationary_ground = (
                on_ground is True
                and (cur_speed is None or cur_speed < GROUND_STATIONARY_KTS)
            )
            if is_stationary_ground:
                alpha = GROUND_STATIONARY_ALPHA
            else:
                # Dead-reckon from previous smoothed position using smoothed heading/speed
                dr_track = result.get("smoothed_track") or s_track_prev
                dr_speed = result.get("smoothed_ground_speed") or s_speed_prev
                if dr_track is not None and dr_speed is not None:
                    pred_lat, pred_lon = self._dead_reckon(s_lat_prev, s_lon_prev, dr_track, dr_speed, dt)
                    deviation_nm = self._flat_distance_nm(cur_lat, cur_lon, pred_lat, pred_lon)
                    alpha = OUTLIER_ALPHA if deviation_nm > POSITION_OUTLIER_NM else SMOOTHING_ALPHA
                else:
                    alpha = SMOOTHING_ALPHA
            result["smoothed_latitude"] = round(self._ema(alpha, cur_lat, s_lat_prev), 4)
            result["smoothed_longitude"] = round(self._ema(alpha, cur_lon, s_lon_prev), 4)
        elif cur_lat is not None and cur_lon is not None:
            result["smoothed_latitude"] = round(cur_lat, 4)
            result["smoothed_longitude"] = round(cur_lon, 4)

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
            inferred["smoothed_latitude"] = round(ac.latitude, 4)
            inferred["smoothed_longitude"] = round(ac.longitude, 4)

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
            if not self._is_position_behind(icao, ac_dict):
                self._update_broadcast_high_water(icao, ac_dict)
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
                        if self._is_position_behind(icao, ac_dict):
                            # Anchor _prev_state to the high-water mark so the
                            # EMA doesn't start from a retreated position next
                            # time, which is what causes visible bouncing.
                            hw = self._broadcast_high_water.get(icao)
                            if hw and icao in self._prev_state:
                                self._prev_state[icao]["s_lat"] = hw.get("lat")
                                self._prev_state[icao]["s_lon"] = hw.get("lon")
                            continue
                        self._update_broadcast_high_water(icao, ac_dict)
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
                self._last_broadcast.pop(icao, None)
                self._broadcast_high_water.pop(icao, None)
                self._dirty_aircraft.discard(icao)
                removed.append(icao)

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
