"""ADS-B message decoder — decodes raw hex Mode S messages using pyModeS.

This module receives raw hex strings (e.g. "8d4840d6583fb20f8175d4b6c540")
from the collector hub and decodes them into aircraft state dicts suitable
for feeding into the AircraftStore.

All SDR/subprocess concerns live in the collector; this module is pure decoding.
"""

import logging

import pyModeS as pms

logger = logging.getLogger(__name__)

# Maximum distance (degrees) from receiver to accept a decoded position.
MAX_DISTANCE_DEG = 5.0


class Decoder:
    """Stateful Mode S decoder that tracks per-aircraft position history
    for reference-based CPR decoding."""

    def __init__(self):
        # Track last known position per aircraft for reference-based decoding
        self._last_pos: dict[str, tuple[float, float]] = {}
        # Set of known ICAOs (from DF11/DF17/DF18) used to validate short messages
        self._known_icaos: set[str] = set()

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
        """Decode a 112-bit ADS-B Extended Squitter (DF17/DF18)."""
        try:
            df = pms.df(msg)
        except Exception:
            return None

        if df not in (17, 18):
            return None

        if pms.crc(msg) != 0:
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
                result["alt_geom"] = alt
                result["altitude"] = alt
            result["on_ground"] = False
            self._update_cpr(icao, msg, tc, result, ref_lat, ref_lon)

        elif tc == 28:
            result["emergency"] = True

        if len(result) > 1:
            return result
        return None

    def _decode_short(self, msg: str) -> dict | None:
        """Decode a 56-bit Mode S short message (DF0/4/5/11/16/20/21)."""
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

        elif df in (4, 20):
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

        elif df in (5, 21):
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

        elif df in (0, 16):
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
                    result["latitude"] = round(lat, 6)
                    result["longitude"] = round(lon, 6)
                    self._last_pos[icao] = (lat, lon)
                else:
                    logger.debug(
                        "Discarding far position for %s: %.4f, %.4f", icao, lat, lon
                    )
        except Exception as e:
            logger.debug("CPR decode error for %s: %s", icao, e)

        if len(self._last_pos) > 1000:
            self._last_pos.clear()
