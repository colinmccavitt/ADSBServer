# ADS-B Receiver — API Protocol Reference

## Base URL

```
http://<host>:8080
```

---

## Architecture

This server is always a **central aggregation server**: it exposes the HTTP
API/web UI and a raw-TCP `CollectorHub` (default port `4002`, see
[Collector Protocol](#collector-protocol-tcp-port-4002) below) that one or
more remote collectors connect to and stream decoded ADS-B data into. There
is no `--mode` flag or standalone/collector split — a single process always
does both jobs. Running with exactly one collector on the same machine looks
and feels like a "standalone" receiver, but architecturally it's just this
server with one collector attached.

---

## REST API Endpoints

All `/api/*` endpoints require an `X-API-Key` header **unless** no client
keys are configured (open access) or the request comes from the server's
own browser UI — see [Authentication](#authentication).

### Aircraft

| Method | Endpoint                 | Description                          | Response Model         |
| ------ | ------------------------ | ------------------------------------ | ---------------------- |
| `GET`  | `/api/aircraft`          | List all currently tracked aircraft  | `AircraftList`         |
| `GET`  | `/api/aircraft/{icao}`   | Get a single aircraft by ICAO hex    | `Aircraft` or `{"error": ...}` |

### Receiver

| Method | Endpoint          | Description                                       | Response Model |
| ------ | ----------------- | -------------------------------------------------- | --------------- |
| `GET`  | `/api/stats`      | Receiver statistics (uptime, counts)               | `ReceiverStats` |
| `GET`  | `/api/config`     | Get current receiver configuration                 | Config dict     |
| `PUT`  | `/api/config`     | Update receiver config (latitude/longitude only)   | Config dict     |
| `GET`  | `/api/watchlist`  | Get the registration watchlist (**no auth required**) | Watchlist dict |

### Aircraft Database

| Method | Endpoint           | Description                                             | Response Model |
| ------ | ------------------ | -------------------------------------------------------- | -------------- |
| `GET`  | `/api/db/status`   | Aircraft metadata DB status                              | Status dict    |
| `POST` | `/api/db/update`   | Force-refresh the aircraft DB (rate-limited, see below)  | Update status  |

### Aircraft Types

| Method | Endpoint              | Description                        | Response Model |
| ------ | --------------------- | ---------------------------------- | -------------- |
| `GET`  | `/api/types`          | All detected aircraft type codes   | Types list     |
| `GET`  | `/api/types/summary`  | Aggregated type statistics         | Summary dict   |

### Collectors

| Method | Endpoint                     | Description                                     | Response Model      |
| ------ | ---------------------------- | ----------------------------------------------- | ------------------- |
| `GET`  | `/api/collectors`            | List all currently connected remote collectors  | `CollectorInfo[]`   |
| `GET`  | `/api/collectors/{id}`       | Get details for a specific collector            | `CollectorInfo` or `{"error": ...}` |

### Connected clients

| Method | Endpoint         | Description                                     | Response Model            |
| ------ | ---------------- | ------------------------------------------------ | ------------------------- |
| `GET`  | `/api/clients`   | List all currently connected WebSocket clients   | `ConnectedClientInfo[]`   |

### Admin (no auth required)

| Method | Endpoint          | Description                                              |
| ------ | ----------------- | --------------------------------------------------------- |
| `GET`  | `/admin`          | Live HTML dashboard (collectors, clients, stats)          |
| `GET`  | `/admin/stream`   | Server-Sent Events feed backing the dashboard, every 2s   |

> `/api/db/update` is rate-limited to at most once every 5 minutes; requests
> within the cooldown window return `{"status": "cooldown", "retry_after_seconds": ...}`
> without triggering a re-download.

---

## Data Models

### `Aircraft`

```json
{
  "icao": "A1B2C3",
  "callsign": "UAL123",
  "altitude": 35000,
  "alt_geom": 34890,
  "ground_speed": 450.5,
  "track": 270.0,
  "latitude": 38.8560,
  "longitude": -77.0495,
  "vertical_rate": -500,
  "squawk": "1200",
  "alert": false,
  "emergency": false,
  "on_ground": false,
  "message_count": 42,
  "first_seen": "2026-02-16T12:00:00Z",
  "last_seen": "2026-02-16T12:05:30Z",
  "registration": "N12345",
  "aircraft_type": "A320",
  "aircraft_model": "Airbus A320-214",
  "manufacturer": "Airbus",
  "owner_operator": "United Airlines",
  "year_built": "2015",
  "is_military": false,
  "alt_diff": -110,
  "turn_rate": -1.25,
  "speed_trend": 0.03,
  "flight_phase": "descending",
  "distance_nm": 24.7,
  "bearing": 135.2,
  "source_collectors": ["dc-metro", "richmond"],
  "nearest_collector_nm": 12.3
}
```

#### Field Reference

| Field                  | Type               | Nullable | Description                              |
| ---------------------- | ------------------ | -------- | ---------------------------------------- |
| `icao`                 | `string`           | No       | ICAO 24-bit hex address (e.g. `A1B2C3`) |
| `callsign`             | `string`           | Yes      | Flight callsign                          |
| `altitude`             | `integer`          | Yes      | Barometric pressure altitude in feet     |
| `alt_geom`             | `integer`          | Yes      | WGS84 geometric altitude in feet (above ellipsoid) |
| `ground_speed`         | `float`            | Yes      | Ground speed in knots                    |
| `track`                | `float`            | Yes      | Track angle in degrees (0 = North)       |
| `latitude`             | `float`            | Yes      | Latitude in decimal degrees              |
| `longitude`            | `float`            | Yes      | Longitude in decimal degrees             |
| `vertical_rate`        | `integer`          | Yes      | Vertical rate in ft/min                  |
| `squawk`               | `string`           | Yes      | Transponder squawk code                  |
| `alert`                | `boolean`          | Yes      | Alert flag                               |
| `emergency`            | `boolean`          | Yes      | Emergency flag                           |
| `on_ground`            | `boolean`          | Yes      | Whether the aircraft is on the ground    |
| `message_count`        | `integer`          | No       | Total ADS-B messages received            |
| `first_seen`           | `datetime` (ISO)   | No       | Timestamp of first detection             |
| `last_seen`            | `datetime` (ISO)   | No       | Timestamp of most recent message         |
| `registration`         | `string`           | Yes      | Tail number (e.g. `N12345`)             |
| `aircraft_type`        | `string`           | Yes      | ICAO type designator (e.g. `A320`)      |
| `aircraft_model`       | `string`           | Yes      | Full model name                          |
| `manufacturer`         | `string`           | Yes      | Aircraft manufacturer                    |
| `owner_operator`       | `string`           | Yes      | Owner or operator                        |
| `year_built`           | `string`           | Yes      | Year of manufacture                      |
| `is_military`          | `boolean`          | Yes      | Military aircraft flag                   |
| `alt_diff`             | `integer`          | Yes      | GNSS–barometric altitude difference in feet (from TC 19) |
| `turn_rate`            | `float`            | Yes      | Turn rate in deg/sec (positive = right turn, negative = left turn) |
| `speed_trend`          | `float`            | Yes      | Speed change in knots/sec (positive = accelerating) |
| `flight_phase`         | `string`           | Yes      | `"climbing"`, `"descending"`, `"level"`, or `"on_ground"` |
| `distance_nm`          | `float`            | Yes      | Great-circle distance from receiver in nautical miles |
| `bearing`              | `float`            | Yes      | Bearing from receiver to aircraft in degrees (0 = North) |
| `source_collectors`    | `string[]`         | Yes      | Collector IDs currently reporting this aircraft |
| `nearest_collector_nm` | `float`            | Yes      | Distance from nearest reporting collector in nm |

> Nullable fields populate progressively as ADS-B messages are decoded. Position fields (`latitude`, `longitude`) require CPR decoding from multiple messages and may take several seconds to appear.
>
> **Geometric altitude** (`alt_geom`) is the aircraft's height above the WGS84 ellipsoid, as opposed to barometric pressure altitude (`altitude`). It is derived from one of two sources: (1) the GNSS–barometric altitude difference broadcast in ADS-B velocity messages (TC 19), combined with the barometric altitude, or (2) direct GNSS altitude from position messages (TC 20-22). The 3D Cesium view uses `alt_geom` for accurate WGS84 ellipsoidal positioning.
>
> **Inferred fields** are computed server-side from successive ADS-B messages and receiver geometry — they are not part of the ADS-B broadcast itself. `turn_rate` and `speed_trend` require at least two messages to compute. `distance_nm` and `bearing` require both a decoded aircraft position and a configured receiver location. `flight_phase` is derived from `vertical_rate` and `on_ground` status; vertical rates within ±100 ft/min are classified as `"level"`.
>
> **Multi-collector fields** (`source_collectors`, `nearest_collector_nm`) are only populated once at least one remote collector reports the aircraft over the TCP collector protocol.

### `AircraftList`

```json
{
  "count": 12,
  "aircraft": [ ]
}
```

| Field      | Type              | Description                    |
| ---------- | ----------------- | ------------------------------ |
| `count`    | `integer`         | Number of tracked aircraft     |
| `aircraft` | `Aircraft[]`      | Array of Aircraft objects      |

### `ReceiverStats`

```json
{
  "uptime_seconds": 3600.5,
  "aircraft_count": 12,
  "aircraft_with_position": 9,
  "messages_total": 48230,
  "messages_per_second": 13.4
}
```

| Field                     | Type      | Description                              |
| ------------------------- | --------- | ---------------------------------------- |
| `uptime_seconds`          | `float`   | Seconds since the receiver started       |
| `aircraft_count`          | `integer` | Total aircraft currently tracked         |
| `aircraft_with_position`  | `integer` | Aircraft that have a decoded position    |
| `messages_total`          | `integer` | Cumulative messages received             |
| `messages_per_second`     | `float`   | Current message rate                     |

### `ReceiverConfig`

Used as the request body for `PUT /api/config`. Only the receiver's
position is configurable via the API — there is no tuner/gain setting since
this server does not talk to an SDR directly (that's the collector's job).

```json
{
  "latitude": 38.8560,
  "longitude": -77.0495
}
```

| Field       | Type    | Description                          |
| ----------- | ------- | ------------------------------------ |
| `latitude`  | `float` | Receiver latitude (decimal degrees)  |
| `longitude` | `float` | Receiver longitude (decimal degrees) |

### `CollectorInfo`

Returned by `GET /api/collectors`.

```json
{
  "collector_id": "dc-metro",
  "name": "DC Metro",
  "latitude": 38.856,
  "longitude": -77.050,
  "connected_since": "2026-02-16T08:00:00Z",
  "aircraft_count": 42,
  "messages_per_second": 13.4,
  "last_heartbeat": "2026-02-16T12:05:20Z"
}
```

| Field                 | Type             | Description                              |
| --------------------- | ---------------- | ---------------------------------------- |
| `collector_id`        | `string`         | Unique collector identifier              |
| `name`                | `string`         | Human-friendly name (nullable)           |
| `latitude`            | `float`          | Collector receiver latitude (nullable)   |
| `longitude`           | `float`          | Collector receiver longitude (nullable)  |
| `connected_since`     | `datetime` (ISO) | When the collector connected             |
| `aircraft_count`      | `integer`        | Aircraft currently reported              |
| `messages_per_second` | `float`          | Current message rate                     |
| `last_heartbeat`      | `datetime` (ISO) | Last heartbeat timestamp                 |

---

## Configuration

### `config.json`

Non-secret settings, safe to commit to version control.

```json
{
  "latitude": 38.856,
  "longitude": -77.050,
  "http_port": 8080,
  "collector_port": 4002,
  "watchlist": ["N12345"]
}
```

| Field            | Type       | Default        | Description                                          |
| ---------------- | ---------- | -------------- | ----------------------------------------------------- |
| `latitude`       | `float`    | `38.855...`    | Receiver latitude, used for distance/bearing calcs    |
| `longitude`      | `float`    | `-77.049...`   | Receiver longitude, used for distance/bearing calcs   |
| `http_port`      | `integer`  | `8080`         | Port for the HTTP/WebSocket API and web UI            |
| `collector_port` | `integer`  | `4002`         | TCP port the `CollectorHub` listens on                |
| `watchlist`      | `string[]` | `[]`           | Registrations to highlight in the web UI (no auth required to read) |

### `config.secrets.json` (gitignored)

API keys live in a **separate file, excluded from git**, so they can never
be committed by accident. Copy `config.secrets.example.json` to
`config.secrets.json` and fill in real values.

```json
{
  "collector_keys": ["<uuid>", "<uuid>"],
  "client_keys": ["<uuid>", "<uuid>"]
}
```

| Field            | Type       | Description                                        |
| ---------------- | ---------- | --------------------------------------------------- |
| `collector_keys` | `string[]` | Valid API keys for TCP collector connections        |
| `client_keys`    | `string[]` | Valid API keys for REST API access (CLI/scripts)    |

**Backward compatibility:** if `config.secrets.json` is absent, the server
falls back to reading an `api_keys` block from `config.json` (older
layout), and if that's also absent or both arrays are empty, all
authentication is disabled (open access).

---

## WebSocket Protocol

### Browser WebSocket — `/ws`

```
ws://<host>:8080/ws
```

The connection is persistent. The server pushes JSON messages to all connected clients whenever aircraft state changes. No client-to-server messages are required after the initial handshake.

#### Message Types

##### 1. `update` — Aircraft state change

Sent whenever an aircraft's state is updated (new position, altitude, callsign, etc.). Includes the full aircraft object and current receiver stats.

```json
{
  "type": "update",
  "aircraft": {
    "icao": "A1B2C3",
    "callsign": "UAL123",
    "altitude": 35000,
    "alt_geom": 34890,
    "ground_speed": 450.5,
    "track": 270.0,
    "latitude": 38.8560,
    "longitude": -77.0495,
    "vertical_rate": -500,
    "squawk": "1200",
    "alert": false,
    "emergency": false,
    "on_ground": false,
    "message_count": 42,
    "first_seen": "2026-02-16T12:00:00Z",
    "last_seen": "2026-02-16T12:05:30Z",
    "registration": "N12345",
    "aircraft_type": "A320",
    "aircraft_model": "Airbus A320-214",
    "manufacturer": "Airbus",
    "owner_operator": "United Airlines",
    "year_built": "2015",
    "is_military": false,
    "alt_diff": -110,
    "turn_rate": -1.25,
    "speed_trend": 0.03,
    "flight_phase": "descending",
    "distance_nm": 24.7,
    "bearing": 135.2,
    "source_collectors": ["dc-metro", "richmond"],
    "nearest_collector_nm": 12.3
  },
  "stats": {
    "uptime_seconds": 3600.5,
    "aircraft_count": 12,
    "aircraft_with_position": 9,
    "messages_total": 48230,
    "messages_per_second": 13.4
  }
}
```

##### 2. `remove` — Aircraft pruned

Sent when an aircraft has not been heard from in **60 seconds** and is removed from tracking.

```json
{
  "type": "remove",
  "icao": "A1B2C3"
}
```

There is no `autogain` message type — this server does not drive an SDR's
tuner gain directly (that is the collector's concern, outside this repo).

---

## Collector Protocol (TCP, port 4002)

Collectors do **not** use a WebSocket. They open a plain TCP connection to
`collector_port` (default `4002`) and speak a minimal line-based protocol
handled by `CollectorHub`:

```
<host>:4002
```

### 1. Hello line (JSON, first line only)

Immediately after connecting, the collector must send exactly one line of
JSON metadata, terminated with `\n`:

```json
{"id": "dc-metro", "name": "DC Metro Receiver", "lat": 38.856, "lon": -77.050, "api_key": "your-collector-key-here"}
```

| Field     | Type     | Required | Description                                          |
| --------- | -------- | -------- | ----------------------------------------------------- |
| `id`      | `string` | Yes      | Unique collector identifier                            |
| `name`    | `string` | No       | Human-friendly name, shown in the admin dashboard/UI   |
| `lat`     | `float`  | No       | Collector's receiver latitude (used as CPR reference)  |
| `lon`     | `float`  | No       | Collector's receiver longitude (used as CPR reference) |
| `api_key` | `string` | If configured | Must match one of `config.secrets.json`'s `collector_keys` |

If `api_key` is missing or invalid (and collector keys are configured), the
server replies with a single JSON line and closes the connection:

```json
{"error": "invalid_api_key"}
```

If the hello line doesn't arrive within **10 seconds**, or isn't valid
JSON, the connection is dropped.

### 2. Raw hex message stream

After the hello line, the collector streams raw Mode S messages — one
uppercase or lowercase hex string per line, optionally wrapped in
`*...;` framing (dump1090-style), 14 hex chars (56-bit / DF0,4,5,11) or 28
hex chars (112-bit / DF16-21):

```
*8D4840D6202CC371C32CE0576098;
8d4840d6584c5e6bfceb9d4e63bd
```

The server decodes each line with `pyModeS`, applies single-bit CRC error
correction for DF17/18, updates the shared `AircraftStore`, and tags the
resulting aircraft with this collector's `id` in `source_collectors`.

There is no explicit `heartbeat` or `aircraft_remove` message — the server
infers collector health from message rate (`messages_per_second`, exposed
via `GET /api/collectors`) and prunes aircraft after the shared 60-second
stale timeout regardless of which collector(s) reported them.

### Example collector (Python)

```python
import json
import socket

sock = socket.create_connection(("central-server", 4002))
sock.sendall((json.dumps({
    "id": "my-collector",
    "name": "My Receiver",
    "lat": 38.856,
    "lon": -77.050,
    "api_key": "your-collector-key-here",
}) + "\n").encode())

for hex_msg in read_hex_messages_from_sdr():  # e.g. from dump1090 --net
    sock.sendall((hex_msg + "\n").encode())
```

---

## Key Behaviors

| Behavior             | Detail                                                                                       |
| -------------------- | -------------------------------------------------------------------------------------------- |
| **Stale timeout**    | Aircraft are removed after 60 seconds with no messages                                       |
| **Enrichment**       | Metadata (registration, type, operator) is enriched from a local ADS-B Exchange DB (~615k aircraft), with hexdb.io as fallback |
| **DB auto-refresh**  | The aircraft metadata DB auto-refreshes when older than 7 days                               |
| **Units**            | Altitude: **feet** (barometric & geometric), Speed: **knots**, Track: **degrees** (0=N), Vertical rate: **ft/min**, Coordinates: **decimal degrees** |
| **ICAO codes**       | 24-bit hex addresses, uppercase (e.g. `A1B2C3`)                                             |
| **Multi-collector**  | The same aircraft seen by multiple collectors is merged — most recent data wins, `source_collectors` tracks all reporting collectors |

---

## Recommended Client Integration

1. **Bootstrap** — `GET /api/aircraft` to load the current tracked aircraft set.
2. **Subscribe** — Open a WebSocket connection to `/ws`.
3. **On `update`** — Upsert the aircraft object in local state using `icao` as the key.
4. **On `remove`** — Delete the aircraft from local state by `icao`.
5. **Polling fallback** — If WebSocket is unavailable, poll `GET /api/aircraft` every 1–2 seconds.

---

## Example Requests

### cURL

```bash
# Get all aircraft
curl http://localhost:8080/api/aircraft

# Get a single aircraft
curl http://localhost:8080/api/aircraft/A1B2C3

# Get receiver stats
curl http://localhost:8080/api/stats

# Get receiver config
curl http://localhost:8080/api/config

# Update receiver config
curl -X PUT http://localhost:8080/api/config \
  -H "Content-Type: application/json" \
  -d '{"latitude": 38.856, "longitude": -77.050}'

# Check aircraft DB status
curl http://localhost:8080/api/db/status

# Force DB update (rate-limited to once every 5 minutes)
curl -X POST http://localhost:8080/api/db/update

# Get detected types
curl http://localhost:8080/api/types

# Get type summary
curl http://localhost:8080/api/types/summary

# List connected collectors
curl http://localhost:8080/api/collectors

# Get a specific collector
curl http://localhost:8080/api/collectors/dc-metro

# List connected WebSocket clients
curl http://localhost:8080/api/clients

# All of the above require -H "X-API-Key: <key>" unless client_keys is
# empty/unset in config.secrets.json.
```

### WebSocket (wscat)

```bash
# Browser updates
wscat -c ws://localhost:8080/ws
```

### JavaScript

```javascript
// REST
const res = await fetch("http://localhost:8080/api/aircraft");
const { count, aircraft } = await res.json();

// WebSocket
const ws = new WebSocket("ws://localhost:8080/ws");
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  switch (msg.type) {
    case "update":
      upsertAircraft(msg.aircraft);
      updateStats(msg.stats);
      break;
    case "remove":
      removeAircraft(msg.icao);
      break;
  }
};
```

### Python

```python
import httpx
import asyncio
import websockets
import json

# REST
resp = httpx.get("http://localhost:8080/api/aircraft")
data = resp.json()

# WebSocket
async def listen():
    async with websockets.connect("ws://localhost:8080/ws") as ws:
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "update":
                print(f"Aircraft {msg['aircraft']['icao']} updated")
            elif msg["type"] == "remove":
                print(f"Aircraft {msg['icao']} removed")

asyncio.run(listen())
```

### Collector Setup (Python)

See [Collector Protocol](#collector-protocol-tcp-port-4002) — collectors
speak plain TCP, not WebSocket:

```python
import json
import socket

sock = socket.create_connection(("central-server", 4002))
sock.sendall((json.dumps({
    "id": "my-collector",
    "name": "My Receiver",
    "lat": 38.856,
    "lon": -77.050,
    "api_key": "your-collector-key-here",
}) + "\n").encode())

# Then stream raw hex Mode S messages, one per line, e.g. from dump1090's
# --net-ro-port 30005 raw output or an RTL-SDR + pyModeS pipeline.
sock.sendall(b"8d4840d6584c5e6bfceb9d4e63bd\n")
```
