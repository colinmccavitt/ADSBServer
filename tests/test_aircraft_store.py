"""Tests for app.aircraft_store.AircraftStore — update logic, on-ground
altitude clamp, flight-phase classification, distance/bearing math, and
stale-aircraft pruning. Live Mode-S path is a raw passthrough: no altitude
snap, no EMA/dead-reckon smoothing, no backward-position suppression."""

from datetime import datetime, timedelta

import pytest

from app.aircraft_store import AircraftStore


@pytest.fixture
def store():
    return AircraftStore()


async def test_new_aircraft_creates_entry(store):
    await store.update({"icao": "ABC123", "altitude": 35000, "latitude": 40.0, "longitude": -75.0})
    ac = await store.get_by_icao("ABC123")
    assert ac is not None
    assert ac.altitude == 35000
    assert ac.latitude == 40.0
    assert ac.message_count == 1


async def test_second_update_merges_and_increments_message_count(store):
    await store.update({"icao": "ABC123", "altitude": 35000})
    await store.update({"icao": "ABC123", "callsign": "UAL123"})
    ac = await store.get_by_icao("ABC123")
    assert ac.altitude == 35000  # preserved from first update
    assert ac.callsign == "UAL123"
    assert ac.message_count == 2


async def test_altitude_not_snapped_live_modes(store):
    """Live Mode-S truth: raw altitude is stored without 25 ft snap."""
    await store.update({"icao": "ABC123", "altitude": 35012})
    ac = await store.get_by_icao("ABC123")
    assert ac.altitude == 35012


async def test_icao_normalized_upper(store):
    await store.update({"icao": "abc123", "altitude": 1000})
    assert await store.get_by_icao("ABC123") is not None
    assert await store.get_by_icao("abc123") is not None


async def test_backward_position_still_broadcasts(store):
    """Raw passthrough: a position that moves 'backward' along track is
    broadcast exactly like any other update — no ghost-suppression, no
    high-water gating. This is the intended trade-off (jitter over filtering)."""
    import time
    from app.aircraft_store import BROADCAST_MIN_INTERVAL

    await store.update({
        "icao": "ABC123",
        "latitude": 40.02,
        "longitude": -75.0,
        "track": 0.0,
        "ground_speed": 100.0,
    })
    # Age the throttle so the next update is eligible to broadcast immediately.
    store._last_broadcast["ABC123"] = time.time() - (BROADCAST_MIN_INTERVAL + 0.05)

    received = []
    store.on_update(lambda ac: received.append(ac))

    # Move south — "behind" the previous fix along a track of 0 (north).
    await store.update({
        "icao": "ABC123",
        "latitude": 40.0,
        "longitude": -75.0,
        "track": 0.0,
        "ground_speed": 100.0,
    })

    assert len(received) == 1
    assert received[0]["latitude"] == 40.0
    assert received[0]["longitude"] == -75.0
    ac = await store.get_by_icao("ABC123")
    assert ac.latitude == 40.0
    assert ac.longitude == -75.0


async def test_position_updated_tracks_position_decodes_only(store):
    """position_updated is the position clock: set on updates that carry a
    lat/lon, frozen across non-position updates (velocity/squawk/altitude),
    while last_seen keeps advancing on every message."""
    # No position yet -> null.
    await store.update({"icao": "ABC123", "altitude": 35000})
    ac = await store.get_by_icao("ABC123")
    assert ac.position_updated is None

    # First position decode stamps it.
    await store.update({"icao": "ABC123", "latitude": 40.0, "longitude": -75.0})
    ac = await store.get_by_icao("ABC123")
    first_stamp = ac.position_updated
    assert first_stamp is not None

    # Non-position update: last_seen advances, position_updated does not.
    await store.update({"icao": "ABC123", "ground_speed": 450.0})
    ac = await store.get_by_icao("ABC123")
    assert ac.position_updated == first_stamp
    assert ac.last_seen >= first_stamp

    # Next position decode advances it.
    await store.update({"icao": "ABC123", "latitude": 40.01, "longitude": -75.0})
    ac = await store.get_by_icao("ABC123")
    assert ac.position_updated >= first_stamp
    assert ac.position_updated == ac.last_seen


async def test_position_updated_set_on_first_message_with_position(store):
    await store.update({"icao": "DEF456", "latitude": 39.0, "longitude": -76.0})
    ac = await store.get_by_icao("DEF456")
    assert ac.position_updated is not None


async def test_negative_altitude_clamped_to_zero_when_on_ground(store):
    await store.update({"icao": "ABC123", "altitude": -75, "on_ground": True})
    ac = await store.get_by_icao("ABC123")
    assert ac.altitude == 0


async def test_unknown_icao_returns_none(store):
    assert await store.get_by_icao("FFFFFF") is None


async def test_prune_stale_removes_old_aircraft(store):
    await store.update({"icao": "ABC123", "altitude": 1000})
    ac = await store.get_by_icao("ABC123")
    # Force the aircraft to look stale without waiting for the real timeout.
    store._aircraft["ABC123"] = ac.model_copy(
        update={"last_seen": datetime.now() - timedelta(seconds=1000)}
    )

    await store._prune_stale()

    assert await store.get_by_icao("ABC123") is None


async def test_prune_stale_keeps_recent_aircraft(store):
    await store.update({"icao": "ABC123", "altitude": 1000})
    await store._prune_stale()
    assert await store.get_by_icao("ABC123") is not None


# ---------------------------------------------------------------------
# Flight phase classification
# ---------------------------------------------------------------------

@pytest.mark.parametrize("vertical_rate,on_ground,expected", [
    (None, True, "on_ground"),
    (500, False, "climbing"),
    (-500, False, "descending"),
    (0, False, "level"),
    (50, False, "level"),  # within +/-100 ft/min threshold
    (None, False, None),
])
def test_classify_flight_phase(vertical_rate, on_ground, expected):
    assert AircraftStore._classify_flight_phase(vertical_rate, on_ground) == expected


# ---------------------------------------------------------------------
# Distance / bearing from receiver
# ---------------------------------------------------------------------

def test_distance_bearing_none_without_receiver_position(store):
    dist, brg = store._distance_bearing(40.0, -75.0)
    assert dist is None
    assert brg is None


def test_distance_bearing_due_north(store):
    store.receiver_lat = 40.0
    store.receiver_lon = -75.0
    # 1 degree of latitude north is ~60nm, bearing should be ~0 (north).
    dist, brg = store._distance_bearing(41.0, -75.0)
    assert dist == pytest.approx(60.0, abs=1.0)
    assert brg == pytest.approx(0.0, abs=0.5)
