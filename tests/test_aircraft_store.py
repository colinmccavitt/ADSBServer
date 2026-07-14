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


async def test_position_behind_force_broadcast_after_silence(store):
    """Alive ICAOs must keep broadcasting even when every packet is 'behind'.

    Regression for the ghost-aircraft bug: `_is_position_behind` used to
    suppress forever while `last_seen` kept refreshing, so clients never
    got `update` OR `remove`. Force-broadcast kicks in past
    BROADCAST_MAX_SILENCE.
    """
    from app.aircraft_store import BROADCAST_MAX_SILENCE
    import time

    await store.update({
        "icao": "ABC123",
        "latitude": 40.0,
        "longitude": -75.0,
        "track": 0.0,
        "ground_speed": 100.0,
    })
    assert "ABC123" in store._last_broadcast

    # Seed a high-water mark AHEAD of the next update so the next packet
    # looks "behind" along track.
    store._broadcast_high_water["ABC123"] = {
        "lat": 40.01, "lon": -75.0, "track": 0.0,
    }
    # Pretend we haven't successfully broadcast in a long time.
    aged = time.time() - (BROADCAST_MAX_SILENCE + 1.0)
    store._last_broadcast["ABC123"] = aged

    received = []
    store.on_update(lambda ac: received.append(ac["icao"]))

    await store.update({
        "icao": "ABC123",
        "latitude": 40.0,       # south of high-water → behind for track=0
        "longitude": -75.0,
        "track": 0.0,
        "ground_speed": 100.0,
    })

    assert received == ["ABC123"], "force-broadcast must fire after silence"
    assert store._last_broadcast["ABC123"] > aged, "successful send must refresh last_broadcast"


async def test_last_broadcast_not_bumped_on_suppressed_attempt(store):
    """A position-behind suppress must NOT refresh `_last_broadcast`.

    Otherwise the throttle thinks we just sent and clients go silent while
    last_seen stays fresh — prune never fires for the web UI / WOPR.
    """
    import time
    from app.aircraft_store import BROADCAST_MIN_INTERVAL

    await store.update({
        "icao": "ABC123",
        "latitude": 40.0,
        "longitude": -75.0,
        "track": 0.0,
        "ground_speed": 100.0,
    })
    # Recent successful broadcast — force path must NOT trigger, but the
    # min-interval throttle must allow another attempt so we exercise the
    # suppress path (not the dirty-set short-circuit).
    store._last_broadcast["ABC123"] = time.time() - (BROADCAST_MIN_INTERVAL + 0.05)
    stamped = store._last_broadcast["ABC123"]
    store._broadcast_high_water["ABC123"] = {
        "lat": 40.01, "lon": -75.0, "track": 0.0,
    }

    received = []
    store.on_update(lambda ac: received.append(ac["icao"]))

    await store.update({
        "icao": "ABC123",
        "latitude": 40.0,
        "longitude": -75.0,
        "track": 0.0,
        "ground_speed": 100.0,
    })

    assert received == [], "behind packet within silence window must stay suppressed"
    assert store._last_broadcast["ABC123"] == stamped, \
        "suppressed attempt must not bump last_broadcast"


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
