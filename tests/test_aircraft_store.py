"""Tests for app.aircraft_store.AircraftStore — update logic, altitude
snapping/clamping, flight-phase classification, distance/bearing math,
EMA smoothing helpers, and stale-aircraft pruning."""

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


async def test_altitude_is_snapped_to_nearest_25_feet(store):
    await store.update({"icao": "ABC123", "altitude": 35012})
    ac = await store.get_by_icao("ABC123")
    assert ac.altitude == 35000


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


# ---------------------------------------------------------------------
# EMA / dead-reckoning helpers
# ---------------------------------------------------------------------

def test_ema_basic_blend():
    assert AircraftStore._ema(0.5, new_val=10, prev_val=0) == 5.0


def test_ema_angle_wraps_shortest_path():
    # 350 -> 10 the "short way" is +20 (via 360/0), not -340.
    result = AircraftStore._ema(0.5, new_val=10, prev_val=350, is_angle=True)
    assert result == pytest.approx(0.0, abs=1e-6)


def test_dead_reckon_moves_north_for_zero_track():
    lat, lon = AircraftStore._dead_reckon(lat=40.0, lon=-75.0, track_deg=0.0, speed_kts=60.0, dt=60.0)
    # 60kt for 60s = 1nm ~= 1/60 degree of latitude.
    assert lat == pytest.approx(40.0 + 1 / 60, abs=1e-3)
    assert lon == pytest.approx(-75.0, abs=1e-6)


def test_flat_distance_nm_one_degree_latitude():
    dist = AircraftStore._flat_distance_nm(40.0, -75.0, 41.0, -75.0)
    assert dist == pytest.approx(60.0, abs=0.5)
