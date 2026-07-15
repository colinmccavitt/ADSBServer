"""ADS-B message decoder — decodes raw hex Mode S messages using pyModeS.

This module receives raw hex strings (e.g. "8d4840d6583fb20f8175d4b6c540")
from the collector hub and decodes them into aircraft state dicts suitable
for feeding into the AircraftStore.

All SDR/subprocess concerns live in the collector; this module is pure decoding.
"""

import logging
from collections import OrderedDict

import pyModeS as pms

logger = logging.getLogger(__name__)

# Maximum distance (degrees) from receiver to accept a decoded position.
MAX_DISTANCE_DEG = 5.0

# Max aircraft tracked in the CPR reference cache. Evicted least-recently-
# updated first (never wiped wholesale — a bulk clear would degrade position
# decoding for every active aircraft at once; see Tickets/DEC-001).
MAX_POSITION_REFS = 1000

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


class Decoder:
    """Stateful Mode S decoder that tracks per-aircraft position history
    for reference-based CPR decoding."""

    def __init__(self, db_icao_check=None):
        # Last known position per aircraft for reference-based decoding,
        # LRU-ordered (most recently updated last) and bounded by
        # MAX_POSITION_REFS with per-entry eviction.
        self._last_pos: OrderedDict[str, tuple[float, float]] = OrderedDict()
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
                speed, heading, vert_rate, _ = velocity_info
                if speed is not None:
                    result["ground_speed"] = speed
                if heading is not None:
                    result["track"] = heading
                if vert_rate is not None:
                    result["vertical_rate"] = vert_rate
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

    def _update_cpr(self, icao: str, msg: str, tc: int, result: dict,
                    ref_lat: float, ref_lon: float):
        """Decode position from a single CPR frame using reference-based decoding.

        Uses the aircraft's last known position (if available) or the collector's
        reported location as the reference point.
        """
        if icao in self._last_pos:
            rlat, rlon = self._last_pos[icao]
        else:
            rlat, rlon = ref_lat, ref_lon

        try:
            if 5 <= tc <= 8:
                pos = pms.adsb.surface_position_with_ref(msg, rlat, rlon)
            else:
                pos = pms.adsb.airborne_position_with_ref(msg, rlat, rlon)

            if pos and pos[0] is not None and pos[1] is not None:
                lat, lon = pos
                if (
                    abs(lat - ref_lat) < MAX_DISTANCE_DEG
                    and abs(lon - ref_lon) < MAX_DISTANCE_DEG
                ):
                    result["latitude"] = round(lat, 4)
                    result["longitude"] = round(lon, 4)
                    self._last_pos[icao] = (lat, lon)
                    self._last_pos.move_to_end(icao)
                    if len(self._last_pos) > MAX_POSITION_REFS:
                        self._last_pos.popitem(last=False)  # evict oldest only
                else:
                    logger.debug(
                        "Discarding far position for %s: %.4f, %.4f", icao, lat, lon
                    )
        except Exception as e:
            logger.debug("CPR decode error for %s: %s", icao, e)
