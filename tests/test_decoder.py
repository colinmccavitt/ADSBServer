"""Tests for app.decoder.Decoder.

Test vectors are well-known example messages used in pyModeS's own
documentation/tests (DF17 identification + airborne position), verified
against the installed pyModeS version directly (see shell exploration in
the accompanying audit) rather than hand-derived.
"""

import pyModeS as pms
import pytest

from app.decoder import Decoder, _CRC_SYNDROME_TABLE

CALLSIGN_MSG = "8D4840D6202CC371C32CE0576098"  # DF17 TC4, icao 4840D6, "KLM1023_"
POSITION_MSG = "8D40621D58C382D690C8AC2863A7"  # DF17 TC11, icao 40621D, altitude 38000
POSITION_REF_LAT = 52.26578017412606
POSITION_REF_LON = 3.938912527901786

# DF17 TC29 target-state-and-status vectors captured live at KDCA on
# 2026-07-15 (ADSBCollector BDS reliability study, _bds_raw_30min.ndjson),
# decoded values verified against pyModeS 2.22.0 directly:
#   TC29_FULL_MSG: JBU frame — sel alt 19008 ft (MCP/FCU), sel hdg
#     147.65625 deg, QNH 1015.2 mb, mode-bits status flag OFF (no nav_modes).
#   TC29_MODES_MSG: sel alt 33024 ft (MCP/FCU), heading status OFF, QNH
#     1013.6 mb, mode bits valid: autopilot + vnav engaged.
TC29_FULL_MSG = "8DABE872EA253875A55C08D6CA34"
TC29_MODES_MSG = "8DA230D6EA409860015F88BFF6BA"

# DF17 TC7 surface position (the 1090MHz Riddle / pyModeS docs example),
# icao 484175: movement 18 kt, ground track 140.625 deg, near Schiphol.
SURFACE_MSG = "8C4841753AAB238733C8CD4020B1"
# Same message with the track-status bit (ME bit 13) cleared and the DF17
# parity recomputed (scripts/_surface_craft_invalid.py): pyModeS then returns
# track=None because the message marks its track as invalid.
SURFACE_MSG_INVALID_TRACK = "8C4841753AA3238733C8CD16C005"
SURFACE_REF_LAT = 51.99
SURFACE_REF_LON = 4.38


def test_decode_callsign_message():
    decoder = Decoder()
    result = decoder.decode(CALLSIGN_MSG, 0.0, 0.0)
    assert result is not None
    assert result["icao"] == "4840D6"
    assert result["callsign"] == "KLM1023"


def test_decode_airborne_position_message():
    decoder = Decoder()
    result = decoder.decode(POSITION_MSG, POSITION_REF_LAT, POSITION_REF_LON)
    assert result is not None
    assert result["icao"] == "40621D"
    assert result["altitude"] == 38000
    assert result["on_ground"] is False
    assert result["latitude"] == pytest.approx(52.2572, abs=1e-3)
    assert result["longitude"] == pytest.approx(3.9194, abs=1e-3)


def test_decode_surface_position_with_movement():
    """TC 5-8 surface messages carry ground speed + track; both must be
    decoded so taxiing aircraft keep rotating after touchdown (TC 19
    airborne velocity stops being transmitted on the ground)."""
    decoder = Decoder()
    result = decoder.decode(SURFACE_MSG, SURFACE_REF_LAT, SURFACE_REF_LON)
    assert result is not None
    assert result["icao"] == "484175"
    assert result["on_ground"] is True
    assert result["altitude"] == 0
    assert result["ground_speed"] == pytest.approx(18, abs=0.5)
    assert result["track"] == pytest.approx(140.625, abs=1e-3)
    assert result["latitude"] == pytest.approx(52.3230, abs=1e-3)
    assert result["longitude"] == pytest.approx(4.7305, abs=1e-3)


def test_decode_surface_position_invalid_track_not_set():
    """When the surface message's track-status bit marks the track invalid,
    pyModeS returns track=None and the decoder must NOT emit a track field
    (a stale/garbage track is worse than none)."""
    decoder = Decoder()
    result = decoder.decode(
        SURFACE_MSG_INVALID_TRACK, SURFACE_REF_LAT, SURFACE_REF_LON
    )
    assert result is not None
    assert result["icao"] == "484175"
    assert result["on_ground"] is True
    assert "track" not in result
    # Movement (speed) is independently valid in this vector and still decodes.
    assert result["ground_speed"] == pytest.approx(18, abs=0.5)


def test_decode_position_keeps_six_decimal_precision():
    """CPR positions must not be quantized below sensor resolution: 4-decimal
    rounding was an ~11 m grid that stair-stepped taxi motion; 6 decimals
    (~0.11 m) preserves everything airborne (~5 m) and surface (~1.25 m)
    CPR can actually resolve."""
    raw_lat, raw_lon = pms.adsb.airborne_position_with_ref(
        POSITION_MSG, POSITION_REF_LAT, POSITION_REF_LON
    )
    decoder = Decoder()
    result = decoder.decode(POSITION_MSG, POSITION_REF_LAT, POSITION_REF_LON)
    assert result["latitude"] == round(raw_lat, 6)
    assert result["longitude"] == round(raw_lon, 6)


def test_decode_tc29_target_state_full():
    """TC29 with altitude + heading + QNH valid (mode-bits status off)."""
    decoder = Decoder()
    result = decoder.decode(TC29_FULL_MSG, 0.0, 0.0)
    assert result is not None
    assert result["icao"] == "ABE872"
    assert result["nav_altitude"] == 19008
    assert result["nav_altitude_src"] == "MCP/FCU"
    assert result["nav_heading"] == pytest.approx(147.656, abs=1e-3)
    assert result["nav_qnh"] == pytest.approx(1015.2, abs=0.1)
    # Mode-bits status flag is off in this frame: no nav_modes key at all
    # (absence, not empty list — the message doesn't vouch for the bits).
    assert "nav_modes" not in result


def test_decode_tc29_target_state_modes():
    """TC29 with valid mode bits (autopilot + vnav engaged) and no
    selected heading (its status bit is off)."""
    decoder = Decoder()
    result = decoder.decode(TC29_MODES_MSG, 0.0, 0.0)
    assert result is not None
    assert result["icao"] == "A230D6"
    assert result["nav_altitude"] == 33024
    assert result["nav_qnh"] == pytest.approx(1013.6, abs=0.1)
    assert "nav_heading" not in result
    assert result["nav_modes"] == ["autopilot", "vnav"]


def test_tc19_ground_speed_subtype_writes_ground_speed_and_track(monkeypatch):
    """TC19 subtypes 1/2 (speed_type GS) must populate ground_speed + track."""
    import app.decoder as decoder_module

    monkeypatch.setattr(
        decoder_module.pms.adsb,
        "velocity",
        lambda msg: (420.0, 275.5, -512, "GS"),
    )
    monkeypatch.setattr(
        decoder_module.pms.adsb, "typecode", lambda msg: 19
    )
    monkeypatch.setattr(decoder_module.pms, "df", lambda msg: 17)
    monkeypatch.setattr(decoder_module.pms, "icao", lambda msg: "ABCDEF")
    monkeypatch.setattr(decoder_module.pms, "crc", lambda msg: 0)

    decoder = Decoder()
    # Use a real DF17 length hex; CRC/icao/tc are mocked above.
    result = decoder.decode(CALLSIGN_MSG, 0.0, 0.0)
    assert result is not None
    assert result["ground_speed"] == 420.0
    assert result["track"] == 275.5
    assert "airspeed" not in result
    assert "heading" not in result
    assert result["vertical_rate"] == -512


def test_tc19_airspeed_subtype_writes_airspeed_and_heading(monkeypatch):
    """TC19 subtypes 3/4 must NOT mislabel airspeed/heading as GS/track."""
    import app.decoder as decoder_module

    monkeypatch.setattr(
        decoder_module.pms.adsb,
        "velocity",
        lambda msg: (250.0, 90.0, 0, "AS"),
    )
    monkeypatch.setattr(
        decoder_module.pms.adsb, "typecode", lambda msg: 19
    )
    monkeypatch.setattr(decoder_module.pms, "df", lambda msg: 17)
    monkeypatch.setattr(decoder_module.pms, "icao", lambda msg: "ABCDEF")
    monkeypatch.setattr(decoder_module.pms, "crc", lambda msg: 0)

    decoder = Decoder()
    result = decoder.decode(CALLSIGN_MSG, 0.0, 0.0)
    assert result is not None
    assert result["airspeed"] == 250.0
    assert result["heading"] == 90.0
    assert "ground_speed" not in result
    assert "track" not in result


def test_decode_rejects_impossible_position_jump(monkeypatch):
    """Speed-implied jump gate (collector-aligned): a teleport in <1s is dropped."""
    import app.decoder as decoder_module
    import time

    decoder = Decoder()
    # Seed a prior fix 0.5s ago so dt is unambiguous (not ~0).
    decoder._last_pos["40621D"] = (
        POSITION_REF_LAT, POSITION_REF_LON, time.time() - 0.5
    )
    monkeypatch.setattr(
        decoder_module.pms.adsb,
        "airborne_position_with_ref",
        # ~1° ≈ 60 nm in 0.5 s ⇒ absurd speed
        lambda msg, rlat, rlon: (POSITION_REF_LAT + 1.0, POSITION_REF_LON),
    )
    result = decoder.decode(POSITION_MSG, POSITION_REF_LAT, POSITION_REF_LON)
    assert result is not None
    assert "latitude" not in result
    assert "longitude" not in result
    assert result["altitude"] == 38000


def test_decode_rejects_position_far_from_reference(monkeypatch):
    """A decoded position far outside MAX_DISTANCE_DEG from the reference
    should be discarded rather than trusted. Reference-based CPR decoding
    always resolves to *some* position near the reference by construction,
    so we mock pyModeS's decode to directly exercise the distance filter
    in Decoder._update_cpr rather than relying on real CPR math to produce
    a far result (it generally won't, even for a "wrong" reference)."""
    import app.decoder as decoder_module

    monkeypatch.setattr(
        decoder_module.pms.adsb, "airborne_position_with_ref",
        lambda msg, rlat, rlon: (rlat + 20.0, rlon + 20.0),
    )

    decoder = Decoder()
    result = decoder.decode(POSITION_MSG, 0.0, 0.0)
    assert result is not None
    assert "latitude" not in result
    assert "longitude" not in result
    # Non-position fields (altitude) are still returned.
    assert result["altitude"] == 38000


@pytest.mark.parametrize("bad_msg", ["", "ZZ", "1234", "8D4840D6"])
def test_decode_returns_none_for_malformed_input(bad_msg):
    decoder = Decoder()
    assert decoder.decode(bad_msg, 0.0, 0.0) is None


def test_decode_returns_none_for_invalid_hex_of_valid_length():
    decoder = Decoder()
    # 28 hex-length string that isn't valid hex at all.
    assert decoder.decode("Z" * 28, 0.0, 0.0) is None
    assert decoder.decode("Z" * 14, 0.0, 0.0) is None


def test_syndrome_table_is_populated_for_all_bit_positions():
    assert len(_CRC_SYNDROME_TABLE) > 0
    # Every single-bit flip should produce a syndrome we can map back to a
    # bit position (syndromes could theoretically collide, but shouldn't
    # for this size message under the Mode S generator polynomial).
    for bit_pos in range(112):
        msg_bytes = bytearray(14)
        byte_idx, bit_idx = bit_pos // 8, 7 - (bit_pos % 8)
        msg_bytes[byte_idx] |= 1 << bit_idx
        syndrome = pms.crc(msg_bytes.hex().upper())
        assert syndrome in _CRC_SYNDROME_TABLE


def test_single_bit_correction_recovers_original_message():
    """Flipping any single bit of a valid message should be correctable
    back to the original via the precomputed syndrome table."""
    decoder = Decoder()
    original = POSITION_MSG.upper()
    assert pms.crc(original) == 0  # sanity check: fixture message is valid

    for bit_pos in (0, 17, 55, 63, 100, 111):
        msg_bytes = bytearray.fromhex(original)
        byte_idx, bit_idx = bit_pos // 8, 7 - (bit_pos % 8)
        msg_bytes[byte_idx] ^= 1 << bit_idx
        flipped = msg_bytes.hex().upper()
        assert pms.crc(flipped) != 0  # corrupted message should fail CRC

        syndrome = pms.crc(flipped)
        corrected = decoder._try_1bit_correction(flipped, syndrome)
        assert corrected == original


def test_decode_uses_correction_for_single_bit_errors():
    """A message corrupted by a single bit flip should still decode
    correctly end-to-end via the Decoder's automatic correction."""
    decoder = Decoder()
    msg_bytes = bytearray.fromhex(POSITION_MSG.upper())
    msg_bytes[10] ^= 0x01  # flip one low bit somewhere in the payload
    corrupted = msg_bytes.hex().upper()

    result = decoder.decode(corrupted, POSITION_REF_LAT, POSITION_REF_LON)
    assert result is not None
    assert result["icao"] == "40621D"


def test_known_icaos_required_for_short_messages():
    """DF0/4/5 short messages should only decode once the ICAO has been
    seen via a DF11/17/18 message (or validated against a DB callback)."""
    decoder = Decoder()
    # A short message won't validate against an empty _known_icaos set and
    # no db_icao_check, so it should be rejected even if otherwise decodable.
    assert decoder._extract_validated_icao("020016A17BD867") is None


def test_position_ref_cache_evicts_oldest_only():
    """The CPR reference cache must evict per-entry (LRU), never wipe every
    active aircraft's reference at once (Tickets/DEC-001)."""
    from app.decoder import MAX_POSITION_REFS

    decoder = Decoder()
    # A real decode establishes the first (oldest) reference.
    decoder.decode(POSITION_MSG, POSITION_REF_LAT, POSITION_REF_LON)
    assert "40621D" in decoder._last_pos

    # Fill the cache to one over the cap with synthetic references.
    import time
    now = time.time()
    for i in range(MAX_POSITION_REFS):
        icao = f"F{i:05X}"
        decoder._last_pos[icao] = (POSITION_REF_LAT, POSITION_REF_LON, now)
        decoder._last_pos.move_to_end(icao)

    # Trigger the eviction path via a real position decode.
    decoder.decode(POSITION_MSG, POSITION_REF_LAT, POSITION_REF_LON)

    assert len(decoder._last_pos) <= MAX_POSITION_REFS + 1
    # The re-decoded aircraft survives; only the least-recent synthetic
    # entries are gone, and the cache was never emptied.
    assert "40621D" in decoder._last_pos
    assert len(decoder._last_pos) > MAX_POSITION_REFS // 2
