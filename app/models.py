from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class Aircraft(BaseModel):
    """Represents a tracked aircraft and its latest known state."""

    icao: str = Field(..., description="ICAO 24-bit hex address (e.g. 'A1B2C3')")
    callsign: Optional[str] = Field(None, description="Flight callsign (e.g. 'UAL123')")
    altitude: Optional[int] = Field(None, description="Barometric pressure altitude in feet")
    alt_geom: Optional[int] = Field(None, description="WGS84 geometric altitude in feet (above ellipsoid)")
    geo_minus_baro: Optional[int] = Field(None, description="Geometric minus barometric altitude in feet (GNSS-baro offset, TC19)")
    ground_speed: Optional[float] = Field(None, description="Ground speed in knots (TC19 subtypes 1/2 or surface movement)")
    airspeed: Optional[float] = Field(None, description="Indicated/true airspeed in knots (TC19 subtypes 3/4 only — not ground speed)")
    track: Optional[float] = Field(None, description="Ground track angle in degrees (0=North, course over ground)")
    heading: Optional[float] = Field(None, description="Aircraft heading in degrees (0=North; from TC19 airspeed subtypes or preprocessed ingest — may differ from track due to wind/crab)")
    latitude: Optional[float] = Field(None, description="Latitude in decimal degrees")
    longitude: Optional[float] = Field(None, description="Longitude in decimal degrees")
    vertical_rate: Optional[int] = Field(None, description="Vertical rate in ft/min")
    squawk: Optional[str] = Field(None, description="Squawk code (e.g. '7700')")
    alert: Optional[bool] = Field(None, description="Alert flag (squawk change)")
    emergency: Optional[bool] = Field(None, description="Emergency flag")
    on_ground: Optional[bool] = Field(None, description="Ground status")
    message_count: int = Field(0, description="Total messages received from this aircraft")
    first_seen: datetime = Field(default_factory=datetime.now, description="When first detected")
    last_seen: datetime = Field(default_factory=datetime.now, description="When last message received")
    # last_seen advances on EVERY decoded message (velocity, squawk,
    # altitude-only...), so it is the wrong clock for position-rate checks:
    # after a gap in position decodes a fresh fix lands with a nearly-unchanged
    # last_seen and looks like a teleport. position_updated advances ONLY when
    # latitude/longitude actually change from a decoded position message.
    position_updated: Optional[datetime] = Field(None, description="When the position (latitude/longitude) was last updated; null until the first position decode")

    # Target state & status (DF17 TC29): autopilot-selected intent. Field
    # names follow the readsb/wiki convention (nav_*). Published only when
    # the message's status bits vouch for the value.
    nav_altitude: Optional[int] = Field(None, description="Autopilot-selected altitude in feet (TC29; MCP/FCU or FMS source, see nav_altitude_src)")
    nav_altitude_src: Optional[str] = Field(None, description="Source of nav_altitude: 'MCP/FCU' (autopilot panel) or 'FMS'")
    nav_heading: Optional[float] = Field(None, description="Autopilot-selected heading in degrees (TC29)")
    nav_qnh: Optional[float] = Field(None, description="Altimeter barometric pressure setting (QNH) in millibars (TC29)")
    nav_modes: Optional[list[str]] = Field(None, description="Engaged autopilot modes when the TC29 status bit is valid — subset of ['autopilot','vnav','althold','approach','lnav']; empty list = bits valid, nothing engaged")

    # Enrichment fields (populated from aircraft database / hexdb.io)
    registration: Optional[str] = Field(None, description="Tail/registration number (e.g. 'N12345')")
    aircraft_type: Optional[str] = Field(None, description="ICAO type code (e.g. 'A320')")
    aircraft_model: Optional[str] = Field(None, description="Full model (e.g. 'A320-214')")
    manufacturer: Optional[str] = Field(None, description="Aircraft manufacturer (e.g. 'Airbus')")
    owner_operator: Optional[str] = Field(None, description="Registered owner or operator")
    year_built: Optional[str] = Field(None, description="Year of manufacture")
    is_military: Optional[bool] = Field(None, description="Military aircraft flag")

    # Inferred fields (computed from successive ADS-B messages and receiver geometry)
    alt_diff: Optional[int] = Field(None, description="GNSS–barometric altitude difference in feet (from TC19)")
    turn_rate: Optional[float] = Field(None, description="Turn rate in deg/sec (positive=right, negative=left)")
    speed_trend: Optional[float] = Field(None, description="Speed change in knots/sec (positive=accelerating)")
    flight_phase: Optional[str] = Field(None, description="Flight phase: 'climbing', 'descending', 'level', or 'on_ground'")
    distance_nm: Optional[float] = Field(None, description="Distance from receiver in nautical miles")
    bearing: Optional[float] = Field(None, description="Bearing from receiver in degrees (0=North)")

    # Provider-baked attitude (computed by a preprocessing collector so live
    # clients — e.g. WOPR5000 — can lerp orientation directly instead of
    # re-deriving it from position. Present only on preprocessed feeds.)
    roll_deg: Optional[float] = Field(None, description="Bank/roll angle in degrees (positive = right-wing-down)")
    pitch_deg: Optional[float] = Field(None, description="Body pitch angle in degrees (nose up positive)")
    gamma_deg: Optional[float] = Field(None, description="Flight-path angle in degrees (climb angle, nose up positive)")
    preprocessed: Optional[bool] = Field(None, description="True when attitude (roll_deg/pitch_deg/gamma_deg) was baked by the collector at decode time (server serves as-is; not display filtering)")

    # Multi-collector fields (populated on the central server only)
    source_collectors: Optional[list[str]] = Field(None, description="Collector IDs currently reporting this aircraft")
    nearest_collector_nm: Optional[float] = Field(None, description="Distance from the nearest reporting collector in nm")


class AircraftList(BaseModel):
    """Response model for the aircraft list endpoint."""

    count: int = Field(..., description="Number of aircraft currently tracked")
    aircraft: list[Aircraft] = Field(..., description="List of tracked aircraft")


class ReceiverStats(BaseModel):
    """Receiver statistics."""

    uptime_seconds: float = Field(..., description="Seconds since the server started")
    aircraft_count: int = Field(..., description="Currently tracked aircraft")
    aircraft_with_position: int = Field(..., description="Aircraft with a valid position")
    messages_total: int = Field(..., description="Total SBS messages received")
    messages_per_second: float = Field(..., description="Average messages per second over last 60s")
    positions_total: int = Field(0, description="Total position messages received")
    positions_per_second: float = Field(0.0, description="Position messages per second over last 60s")


class CollectorInfo(BaseModel):
    """Information about a connected remote collector."""

    collector_id: str = Field(..., description="Unique collector identifier")
    name: Optional[str] = Field(None, description="Human-friendly collector name")
    latitude: Optional[float] = Field(None, description="Collector receiver latitude")
    longitude: Optional[float] = Field(None, description="Collector receiver longitude")
    connected_since: datetime = Field(..., description="When the collector connected")
    aircraft_count: int = Field(0, description="Aircraft currently reported by this collector")
    messages_per_second: float = Field(0.0, description="Message rate from this collector")
    last_heartbeat: datetime = Field(default_factory=datetime.now, description="Last heartbeat timestamp")


class ConnectedClientInfo(BaseModel):
    """Information about a connected WebSocket client (browser or API)."""

    client_id: str = Field(..., description="Unique client connection identifier")
    client_type: str = Field("browser", description="Client type: 'browser' or 'api'")
    remote_addr: str = Field("", description="Remote IP address")
    connected_since: datetime = Field(default_factory=datetime.now, description="When the client connected")
