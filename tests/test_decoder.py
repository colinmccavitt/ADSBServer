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
