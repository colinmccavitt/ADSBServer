"""ADS-B message decoder — decodes raw hex Mode S messages using pyModeS.

This module receives raw hex strings (e.g. "8d4840d6583fb20f8175d4b6c540")
from the collector hub and decodes them into aircraft state dicts suitable
for feeding into the AircraftStore.

All SDR/subprocess concerns live in the collector; this module is pure decoding.

Position acceptance mirrors the collector's tracker gates (ADSBCollector
``tracker.rs``): far-from-receiver reject plus speed-implied jump reject.
The collector's own decode never reaches WOPR — this path is authoritative.
"""

import logging
import math
import time
from collections import OrderedDict

import pyModeS as pms

logger = logging.getLogger(__name__)

# Maximum distance (degrees) from receiver to accept a decoded position.
MAX_DISTANCE_DEG = 5.0

# Max aircraft tracked in the CPR reference cache. Evicted least-recently-
# updated first (never wiped wholesale — a bulk clear would degrade position
# decoding for every active aircraft at once; see Tickets/DEC-001).
MAX_POSITION_REFS = 1000

# Speed-implied jump gates (aligned with ADSBCollector tracker.rs).
# Reject CPR fixes that would require the aircraft to exceed these speeds
# since the previous accepted fix. Stand down after STALE_JUMP_GATE_SEC so a
# track cannot permanently wedge after a gap.
MAX_AIRBORNE_SPEED_KTS = 750.0
MAX_SURFACE_SPEED_KTS = 120.0
STALE_JUMP_GATE_SEC = 10.0
EARTH_RADIUS_NM = 3440.065
# Small slack so CPR quantization + clock jitter doesn't false-reject.
JUMP_SPEED_SLACK = 1.10

# ── 1-bit CRC error correction syndrome table ─────────────────────────────
# Each single-bit error in a 112-bit Mode S message produces a unique 24-bit
# CRC syndrome.  This table maps syndrome → bit position so we can correct
# single-bit errors in DF17/18 messages (the same technique used by dump1090).
_CRC_SYNDROME_TABLE: dict[int, int] = {}

def _build_syndrome_table() -> dict[int, int]:
    table: dict[int, int] = {}
    for bit_pos in range(112):
        msg_bytes = bytearray(14)
        byte_idx = bit_pos // 8
        bit_idx = 7 - (bit_pos % 8)
        msg_bytes[byte_idx] |= (1 << bit_idx)
        hex_msg = msg_bytes.hex().upper()
        syndrome = pms.crc(hex_msg)
        table[syndrome] = bit_pos
    return table

_CRC_SYNDROME_TABLE = _build_syndrome_table()


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    r_lat1, r_lon1 = math.radians(lat1), math.radians(lon1)
    r_lat2, r_lon2 = math.radians(lat2), math.radians(lon2)
    dlat = r_lat2 - r_lat1
    dlon = r_lon2 - r_lon1
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(r_lat1) * math.cos(r_lat2) * math.sin(dlon / 2) ** 2
    )
    return EARTH_RADIUS_NM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class Decoder:
    """Stateful Mode S decoder that tracks per-aircraft position history
    for reference-based CPR decoding."""

    def __init__(self, db_icao_check=None):
        # Last known position per aircraft for reference-based decoding,
        # LRU-ordered (most recently updated last) and bounded by
        # MAX_POSITION_REFS with per-entry eviction. Value is (lat, lon, unix_ts).
        self._last_pos: OrderedDict[str, tuple[float, float, float]] = OrderedDict()
        # Set of known ICAOs (from DF11/DF17/DF18) used to validate short messages
        self._known_icaos: set[str] = set()
        # Optional callable(icao: str) -> bool for validating ICAOs against
        # the aircraft database (enables tracking DF16/20/21-only aircraft)
        self._db_icao_check = db_icao_check

    def decode(self, hex_msg: str, ref_lat: float, ref_lon: float) -> dict | None:
        """Decode a raw hex Mode S message into an aircraft state dict.

        Args:
            hex_msg: Uppercase hex string (14 or 28 chars, no * ; framing).
            ref_lat: Reference latitude for CPR decoding (collector location).
            ref_lon: Reference longitude for CPR decoding (collector location).

        Returns:
            A dict with at least an ``icao`` key and decoded fields,
            or None if the message could not be decoded.
        """
        if len(hex_msg) == 28:
            return self._decode_long(hex_msg, ref_lat, ref_lon)
        elif len(hex_msg) == 14:
            return self._decode_short(hex_msg)
        return None

    def _decode_long(self, msg: str, ref_lat: float, ref_lon: float) -> dict | None:
        """Decode a 112-bit Mode S message (DF17/DF18 ADS-B + DF16/20/21 surveillance replies)."""
        try:
            df = pms.df(msg)
        except Exception:
            return None

        # ── DF16/20/21: 112-bit surveillance replies (altitude / squawk) ──
        if df in (16, 20):
            icao = self._extract_validated_icao(msg)
            if not icao:
                return None
            result: dict = {"icao": icao}
            try:
                alt = pms.common.altcode(msg)
                if alt is not None:
                    result["altitude"] = alt
            except Exception:
                pass
            if len(result) > 1:
                return result
            return None

        if df == 21:
            icao = self._extract_validated_icao(msg)
            if not icao:
                return None
            result: dict = {"icao": icao}
            try:
                squawk = pms.common.idcode(msg)
                if squawk:
                    result["squawk"] = str(squawk)
            except Exception:
                pass
            if len(result) > 1:
                return result
            return None

        # ── DF17/18: ADS-B Extended Squitter ──
        if df not in (17, 18):
            return None

        syndrome = pms.crc(msg)
        if syndrome != 0:
            corrected = self._try_1bit_correction(msg, syndrome)
            if corrected is not None:
                msg = corrected
            else:
                return None

        icao = pms.icao(msg)
        if not icao:
            return None

        icao = icao.upper()
        self._known_icaos.add(icao)

        tc = pms.adsb.typecode(msg)
        if tc is None:
            return None

        result: dict = {"icao": icao}

        if 1 <= tc <= 4:
            callsign = pms.adsb.callsign(msg)
            if callsign:
                result["callsign"] = callsign.strip("_").strip()

        elif 5 <= tc <= 8:
            result["altitude"] = 0
            result["on_ground"] = True
            # Surface position messages also carry movement (ground speed, in
            # knots like TC 19) and ground track. Aircraft stop transmitting
            # TC 19 airborne velocity once on the ground, so without this the
            # track freezes at its last airborne value while taxiing.
            # pyModeS >= 2.x: surface_velocity(msg) -> (spd, trk, 0, 'GS');
            # track is None when the message's track-status bit marks it
            # invalid, and speed is None when the movement field is not
            # available — only publish fields the message vouches for.
            try:
                surface_velocity = pms.adsb.surface_velocity(msg)
            except Exception:
                surface_velocity = None
            if surface_velocity:
                speed, track, _, _ = surface_velocity
                if speed is not None:
                    result["ground_speed"] = speed
                if track is not None:
                    result["track"] = track
            self._update_cpr(icao, msg, tc, result, ref_lat, ref_lon)

        elif 9 <= tc <= 18:
            alt = pms.adsb.altitude(msg)
            if alt is not None:
                result["altitude"] = alt
            result["on_ground"] = False
            self._update_cpr(icao, msg, tc, result, ref_lat, ref_lon)

        elif tc == 19:
            velocity_info = pms.adsb.velocity(msg)
            if velocity_info:
                # pyModeS: (speed, angle, vert_rate, speed_type) where
                # speed_type is "GS" (subtypes 1/2 — ground speed + track)
                # or "AS"/"IAS"/"TAS" (subtypes 3/4 — airspeed + magnetic
                # heading). Never label airspeed/heading as ground_speed/track.
                speed, angle, vert_rate, speed_type = velocity_info[:4]
                if vert_rate is not None:
                    result["vertical_rate"] = vert_rate
                is_ground = (
                    speed_type is None
                    or str(speed_type).upper() in ("GS", "GROUND SPEED", "GROUND_SPEED")
                )
                if is_ground:
                    if speed is not None:
                        result["ground_speed"] = speed
                    if angle is not None:
                        result["track"] = angle
                else:
                    if speed is not None:
                        result["airspeed"] = speed
                    if angle is not None:
                        result["heading"] = angle
            try:
                alt_diff = pms.adsb.altitude_diff(msg)
                if alt_diff is not None:
                    result["alt_diff"] = int(alt_diff)
            except Exception:
                pass

        elif 20 <= tc <= 22:
            alt = pms.adsb.altitude(msg)
            if alt is not None:
                # GNSS geometric altitude only — do not overwrite baro altitude
                # so Mode-S truth keeps baro and geom as distinct channels.
                result["alt_geom"] = alt
            result["on_ground"] = False
            self._update_cpr(icao, msg, tc, result, ref_lat, ref_lon)

        elif tc == 29:
            # TC 29 (Target State & Status): autopilot-selected state —
            # selected altitude + source, selected heading, QNH, and autopilot
            # mode bits. Arrives ~1 Hz per equipped aircraft and lets clients
            # (WOPR5000) anticipate level-offs and turns before they happen.
            # pyModeS resolves the subtype internally and returns None for any
            # field whose status bit marks it invalid — publish only fields
            # the message vouches for (naming follows the readsb convention).
            try:
                sel_alt, sel_alt_src = pms.adsb.selected_altitude(msg)
                if sel_alt is not None:
                    result["nav_altitude"] = int(sel_alt)
                    # "MCP/FCU" (autopilot panel) or "FMS"
                    result["nav_altitude_src"] = sel_alt_src
            except Exception:
                pass
            try:
                sel_hdg = pms.adsb.selected_heading(msg)
                if sel_hdg is not None:
                    result["nav_heading"] = sel_hdg
            except Exception:
                pass
            try:
                qnh = pms.adsb.baro_pressure_setting(msg)
                if qnh is not None:
                    result["nav_qnh"] = qnh
            except Exception:
                pass
            try:
                # Each returns None when the message's mode-bits-status flag
                # is off (common), else a bool. Publish the engaged-mode list
                # only when the status flag vouches for the bits; an empty
                # list then means "bits valid, nothing engaged".
                mode_flags = {
                    "autopilot": pms.adsb.autopilot(msg),
                    "vnav": pms.adsb.vnav_mode(msg),
                    "althold": pms.adsb.altitude_hold_mode(msg),
                    "approach": pms.adsb.approach_mode(msg),
                    "lnav": pms.adsb.lnav_mode(msg),
                }
                if any(v is not None for v in mode_flags.values()):
                    result["nav_modes"] = [k for k, v in mode_flags.items() if v]
            except Exception:
                pass

        elif tc == 28:
            # TC 28 (Aircraft Status) covers two subtypes: ST1 is emergency/
            # priority status (most of which report "no emergency"), ST2 is a
            # TCAS/ACAS RA broadcast unrelated to emergencies. Treat it as an
            # emergency only when pyModeS says the aircraft actually declared
            # one, not just because a status message was received.
            try:
                result["emergency"] = pms.adsb.is_emergency(msg)
                if result["emergency"]:
                    result["squawk"] = pms.adsb.emergency_squawk(msg)
            except Exception:
                pass

        if len(result) > 1:
            return result
        return None

    def _decode_short(self, msg: str) -> dict | None:
        """Decode a 56-bit Mode S short message (DF0/4/5/11)."""
        try:
            df = pms.df(msg)
        except Exception:
            return None

        if df == 11:
            if pms.crc(msg) != 0:
                return None
            icao = pms.icao(msg)
            if not icao:
                return None
            icao = icao.upper()
            self._known_icaos.add(icao)
            return {"icao": icao}

        elif df == 4:
            icao = self._extract_validated_icao(msg)
            if not icao:
                return None
            result: dict = {"icao": icao}
            try:
                alt = pms.common.altcode(msg)
                if alt is not None:
                    result["altitude"] = alt
            except Exception:
                pass
            if len(result) > 1:
                return result
            return None

        elif df == 5:
            icao = self._extract_validated_icao(msg)
            if not icao:
                return None
            result: dict = {"icao": icao}
            try:
                squawk = pms.common.idcode(msg)
                if squawk:
                    result["squawk"] = str(squawk)
            except Exception:
                pass
            if len(result) > 1:
                return result
            return None

        elif df == 0:
            icao = self._extract_validated_icao(msg)
            if not icao:
                return None
            result: dict = {"icao": icao}
            try:
                alt = pms.common.altcode(msg)
                if alt is not None:
                    result["altitude"] = alt
            except Exception:
                pass
            if len(result) > 1:
                return result
            return None

        return None

    @staticmethod
    def _try_1bit_correction(msg: str, syndrome: int) -> str | None:
        """Attempt to correct a single-bit error in a DF17/18 message.

        Uses the precomputed syndrome table to identify the flipped bit.
        Returns the corrected hex message, or None if the syndrome does
        not correspond to a single-bit error.
        """
        bit_pos = _CRC_SYNDROME_TABLE.get(syndrome)
        if bit_pos is None:
            return None
        msg_bytes = bytearray.fromhex(msg)
        byte_idx = bit_pos // 8
        bit_idx = 7 - (bit_pos % 8)
        msg_bytes[byte_idx] ^= (1 << bit_idx)
        corrected = msg_bytes.hex().upper()
        if pms.crc(corrected) != 0:
            return None
        return corrected

    def _extract_validated_icao(self, msg: str) -> str | None:
        """Extract ICAO from a Mode S message's Address/Parity field.

        For DF0/4/5/16/20/21 the ICAO is XORed into the parity bits.
        We validate it against our set of known ICAOs to reject false decodes.
        """
        try:
            icao = pms.icao(msg)
        except Exception:
            return None
        if not icao:
            return None
        icao = icao.upper()
        if icao in self._known_icaos:
            return icao
        # Check against aircraft database if available
        if self._db_icao_check and self._db_icao_check(icao):
            self._known_icaos.add(icao)
            return icao
        return None

    def _jump_sane(
        self, icao: str, lat: float, lon: float, *, surface: bool, now: float
    ) -> bool:
        """Reject fixes that imply an impossible ground speed since last fix.

        Matches ADSBCollector ``Tracker::airborne_jump_sane`` /
        ``surface_jump_sane`` so the authoritative server decode is no weaker
        than the collector's local tracker.
        """
        prev = self._last_pos.get(icao)
        if prev is None:
            return True
        plat, plon, pts = prev
        dt = now - pts
        if dt <= 0.0 or dt >= STALE_JUMP_GATE_SEC:
            return True
        dist_nm = _haversine_nm(plat, plon, lat, lon)
        max_kts = MAX_SURFACE_SPEED_KTS if surface else MAX_AIRBORNE_SPEED_KTS
        implied_kts = dist_nm / (dt / 3600.0)
        return implied_kts <= max_kts * JUMP_SPEED_SLACK

    def _update_cpr(self, icao: str, msg: str, tc: int, result: dict,
                    ref_lat: float, ref_lon: float):
        """Decode position from a single CPR frame using reference-based decoding.

        Uses the aircraft's last known position (if available) or the collector's
        reported location as the reference point. Applies far-from-receiver and
        speed-implied jump gates (collector-aligned).
        """
        if icao in self._last_pos:
            rlat, rlon, _ = self._last_pos[icao]
        else:
            rlat, rlon = ref_lat, ref_lon

        try:
            surface = 5 <= tc <= 8
            if surface:
                pos = pms.adsb.surface_position_with_ref(msg, rlat, rlon)
            else:
                pos = pms.adsb.airborne_position_with_ref(msg, rlat, rlon)

            if pos and pos[0] is not None and pos[1] is not None:
                lat, lon = pos
                if not (
                    abs(lat - ref_lat) < MAX_DISTANCE_DEG
                    and abs(lon - ref_lon) < MAX_DISTANCE_DEG
                ):
                    logger.debug(
                        "Discarding far position for %s: %.4f, %.4f", icao, lat, lon
                    )
                    return
                now = time.time()
                if not self._jump_sane(icao, lat, lon, surface=surface, now=now):
                    logger.debug(
                        "Discarding jump for %s: %.6f, %.6f", icao, lat, lon
                    )
                    return
                # 6 decimals ≈ 0.11 m — below CPR resolution (airborne
                # ~5 m, surface ~1.25 m), so no real precision is lost.
                # 4 decimals was an ~11 m × 8.7 m grid at DCA latitude,
                # which stair-stepped taxi motion (2-15 m fixes snapped
                # to the same cell).
                result["latitude"] = round(lat, 6)
                result["longitude"] = round(lon, 6)
                self._last_pos[icao] = (lat, lon, now)
                self._last_pos.move_to_end(icao)
                if len(self._last_pos) > MAX_POSITION_REFS:
                    self._last_pos.popitem(last=False)  # evict oldest only
        except Exception as e:
            logger.debug("CPR decode error for %s: %s", icao, e)
