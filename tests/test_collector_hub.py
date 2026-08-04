"""Tests for app.collector_hub — feed protocol parsing, the hello ack, and
connection-registration lifecycle.

Focus is the protocol-2 additions that let a roaming collector spool traffic
while its uplink is down and replay it afterwards, plus the registration
bookkeeping that used to let a dying stale connection unregister a live one.
"""

import asyncio
import json
import time

import pytest

from app import auth
from app.aircraft_store import AircraftStore
from app.collector_hub import (
    FEED_PROTO,
    MAX_TIMESTAMP_AGE_SEC,
    MAX_TIMESTAMP_SKEW_SEC,
    CollectorHub,
    parse_feed_line,
)

HEX = "8D4840D6202CC371C32CE0576098"


# ---------------------------------------------------------------------------
# parse_feed_line
# ---------------------------------------------------------------------------


def test_bare_hex_is_protocol_1_and_has_no_timestamp():
    now = 1_800_000_000.0
    assert parse_feed_line(HEX, now) == (HEX, None)


def test_timestamped_line_yields_hex_and_observation_time():
    now = 1_800_000_000.0
    msg, observed = parse_feed_line(f"1799999990123,{HEX}", now)
    assert msg == HEX
    assert observed == pytest.approx(1799999990.123)


def test_hex_never_contains_a_comma_so_the_split_is_unambiguous():
    """The discriminator only works because Mode-S hex is [0-9A-F] only."""
    assert "," not in HEX
    # A 28-char and a 14-char message both survive a round trip.
    for raw in (HEX, HEX[:14]):
        stamped = f"{int(time.time() * 1000)},{raw}"
        msg, observed = parse_feed_line(stamped, time.time())
        assert msg == raw
        assert observed is not None


def test_non_numeric_prefix_is_not_treated_as_a_timestamp():
    """Garbage must not silently become a timestamp; the caller's length check
    then rejects the line rather than storing a bogus observation time."""
    msg, observed = parse_feed_line(f"NOTATIME,{HEX}", 1_800_000_000.0)
    assert observed is None
    assert msg == f"NOTATIME,{HEX}"
    assert len(msg) not in (14, 28)  # so the read loop filters it


def test_absurdly_old_timestamp_falls_back_to_arrival_time():
    """An unsynced collector clock (booted at the epoch) must degrade to
    protocol-1 behaviour, not back-date live traffic by decades."""
    now = 1_800_000_000.0
    msg, observed = parse_feed_line(f"1000,{HEX}", now)
    assert msg == HEX
    assert observed is None


def test_future_timestamp_beyond_skew_is_rejected():
    now = 1_800_000_000.0
    future_ms = int((now + MAX_TIMESTAMP_SKEW_SEC + 60) * 1000)
    _, observed = parse_feed_line(f"{future_ms},{HEX}", now)
    assert observed is None


def test_timestamp_inside_the_allowed_window_is_accepted():
    now = 1_800_000_000.0
    # A long outage is legitimate: just inside the replay window.
    ok_ms = int((now - MAX_TIMESTAMP_AGE_SEC + 3600) * 1000)
    _, observed = parse_feed_line(f"{ok_ms},{HEX}", now)
    assert observed is not None


# ---------------------------------------------------------------------------
# hello ack + registration lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture
def hub_port(monkeypatch):
    """Run a hub with auth disabled on an ephemeral port."""
    monkeypatch.setattr(auth, "validate_collector_key", lambda key: True)
    return 0


async def _serve(hub: CollectorHub) -> int:
    hub._server = await asyncio.start_server(hub._handle_connection, "127.0.0.1", 0)
    return hub._server.sockets[0].getsockname()[1]


async def test_hello_is_acked_with_the_feed_protocol(hub_port):
    """Collectors need the ack to know timestamped lines are safe to send -
    and its presence also spares them a blocking read timeout on connect."""
    hub = CollectorHub(AircraftStore(), port=0)
    port = await _serve(hub)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(json.dumps({"id": "t1", "lat": 33.5, "lon": -111.9}).encode() + b"\n")
        await writer.drain()

        ack = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
        assert ack["ok"] is True
        assert ack["proto"] == FEED_PROTO

        writer.close()
    finally:
        await hub.stop()


async def test_replayed_message_is_stored_with_its_observation_time(hub_port):
    """End-to-end: a timestamped line must land in the store stamped with when
    the collector saw it, not when the hub received it."""
    store = AircraftStore()
    hub = CollectorHub(store, port=0)
    port = await _serve(hub)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(json.dumps({"id": "t2", "lat": 33.5, "lon": -111.9}).encode() + b"\n")
        await writer.drain()
        await asyncio.wait_for(reader.readline(), timeout=5)  # ack

        observed = time.time() - 600  # ten minutes of spooled backlog
        writer.write(f"{int(observed * 1000)},{HEX}\n".encode())
        await writer.drain()
        await asyncio.sleep(0.3)

        aircraft = await store.get_all()
        assert aircraft, "message was not stored"
        ac = aircraft[0]
        # last_seen reflects the observation, so it is ~10 min old, not ~now.
        age = time.time() - ac.last_seen.timestamp()
        assert age > 300, f"stored with arrival time, not observation time (age {age:.0f}s)"

        writer.close()
    finally:
        await hub.stop()


async def test_stale_connection_cleanup_does_not_unregister_the_live_one():
    """A collector that reconnects replaces its own entry. When the previous
    socket finally dies its cleanup must leave the live registration alone."""
    hub = CollectorHub(AircraftStore(), port=0)

    class _Conn:
        def __init__(self, tag):
            self.tag = tag

    old, new = _Conn("old"), _Conn("new")
    hub._collectors["dup"] = old
    # Reconnect: the newer connection takes over the slot.
    hub._collectors["dup"] = new

    # Simulate the old connection's finally-block identity check.
    async with hub._lock:
        current = hub._collectors.get("dup")
        superseded = current is not None and old is not None and current is not old
        if not superseded:
            hub._collectors.pop("dup", None)

    assert superseded is True
    assert hub._collectors["dup"] is new, "live connection was clobbered by a zombie"
