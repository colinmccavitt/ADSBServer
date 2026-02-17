# ADS-B Receiver — API Protocol Reference

## Base URL

```
http://<host>:8080
```

---

## Run Modes

The application supports three run modes configured via `--mode` CLI flag or the `mode` field in `config.json`:

| Mode           | Description                                                                                     |
| -------------- | ----------------------------------------------------------------------------------------------- |
| `standalone`   | Default. Full local receiver + API server. Original single-instance behaviour.                   |
| `collector`    | Local receiver + API server + pushes aircraft data upstream to a central server via WebSocket.   |
| `server`       | Central aggregation server. Receives data from remote collectors and serves a unified view. Optionally also runs a local decoder with `--collect`. |

---

## REST API Endpoints

### Aircraft

| Method | Endpoint                 | Description                          | Response Model         |
| ------ | ------------------------ | ------------------------------------ | ---------------------- |
| `GET`  | `/api/aircraft`          | List all currently tracked aircraft  | `AircraftList`         |
| `GET`  | `/api/aircraft/{icao}`   | Get a single aircraft by ICAO hex    | `Aircraft` or 404      |

### Receiver

| Method | Endpoint          | Description                              | Response Model           |
| ------ | ----------------- | ---------------------------------------- | ------------------------ |
| `GET`  | `/api/stats`      | Receiver statistics (uptime, counts)     | `ReceiverStats`          |
| `GET`  | `/api/config`     | Get current receiver configuration       | `ReceiverConfigResponse` |
| `PUT`  | `/api/config`     | Update receiver config (lat/lon/gain)    | `ReceiverConfigResponse` |
| `POST` | `/api/autogain`   | Trigger automatic gain calibration       | Auto-gain result         |

### Aircraft Database

| Method | Endpoint           | Description                        | Response Model |
| ------ | ------------------ | ---------------------------------- | -------------- |
| `GET`  | `/api/db/status`   | Aircraft metadata DB status        | Status dict    |
| `POST` | `/api/db/update`   | Force-refresh the aircraft DB      | Update status  |

### Aircraft Types

| Method | Endpoint              | Description                        | Response Model |
| ------ | --------------------- | ---------------------------------- | -------------- |
| `GET`  | `/api/types`          | All detected aircraft type codes   | Types list     |
| `GET`  | `/api/types/summary`  | Aggregated type statistics         | Summary dict   |

### Collectors (server mode only)

| Method | Endpoint                     | Description                                     | Response Model      |
| ------ | ---------------------------- | ----------------------------------------------- | ------------------- |
| `GET`  | `/api/collectors`            | List all currently connected remote collectors  | `CollectorInfo[]`   |
| `GET`  | `/api/collectors/{id}`       | Get details for a specific collector            | `CollectorInfo`     |

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
| `source_collectors`    | `string[]`         | Yes      | Collector IDs reporting this aircraft (server mode only) |
| `nearest_collector_nm` | `float`            | Yes      | Distance from nearest reporting collector in nm (server mode only) |

> Nullable fields populate progressively as ADS-B messages are decoded. Position fields (`latitude`, `longitude`) require CPR decoding from multiple messages and may take several seconds to appear.
>
> **Geometric altitude** (`alt_geom`) is the aircraft's height above the WGS84 ellipsoid, as opposed to barometric pressure altitude (`altitude`). It is derived from one of two sources: (1) the GNSS–barometric altitude difference broadcast in ADS-B velocity messages (TC 19), combined with the barometric altitude, or (2) direct GNSS altitude from position messages (TC 20-22). The 3D Cesium view uses `alt_geom` for accurate WGS84 ellipsoidal positioning.
>
> **Inferred fields** are computed server-side from successive ADS-B messages and receiver geometry — they are not part of the ADS-B broadcast itself. `turn_rate` and `speed_trend` require at least two messages to compute. `distance_nm` and `bearing` require both a decoded aircraft position and a configured receiver location. `flight_phase` is derived from `vertical_rate` and `on_ground` status; vertical rates within ±100 ft/min are classified as `"level"`.
>
> **Multi-collector fields** (`source_collectors`, `nearest_collector_nm`) are only populated when running in server mode and receiving data from multiple collectors.

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

Used as the request body for `PUT /api/config`.

```json
{
  "latitude": 38.8560,
  "longitude": -77.0495,
  "gain": 12.5
}
```

| Field       | Type    | Description                          |
| ----------- | ------- | ------------------------------------ |
| `latitude`  | `float` | Receiver latitude (decimal degrees)  |
| `longitude` | `float` | Receiver longitude (decimal degrees) |
| `gain`      | `float` | Tuner gain in dB                     |

### `CollectorInfo`

Returned by `GET /api/collectors` (server mode only).

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

```json
{
  "latitude": 38.856,
  "longitude": -77.050,
  "gain": 7.7,
  "mode": "standalone",
  "collector_id": "a1b2c3d4e5f6",
  "collector_name": "DC Metro",
  "server_url": "ws://central-server:8080/collector/ws",
  "api_key": "my-secret-key"
}
```

| Field            | Type     | Default        | Description                                                     |
| ---------------- | -------- | -------------- | --------------------------------------------------------------- |
| `latitude`       | `float`  | `38.855...`    | Receiver latitude                                               |
| `longitude`      | `float`  | `-77.049...`   | Receiver longitude                                              |
| `gain`           | `float`  | `7.7`          | RTL-SDR tuner gain in dB                                        |
| `mode`           | `string` | `"standalone"` | Run mode: `"standalone"`, `"collector"`, or `"server"`          |
| `collector_id`   | `string` | auto-generated | Unique collector identifier (UUID4 hex, auto-generated if null) |
| `collector_name` | `string` | `null`         | Human-friendly name for this collector                          |
| `server_url`     | `string` | `null`         | Central server WebSocket URL (collector mode)                   |
| `api_key`        | `string` | `null`         | Shared secret for collector authentication                      |

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

##### 3. `autogain` — Gain calibration progress

Sent during an auto-gain test triggered via `POST /api/autogain`.

**In progress:**

```json
{
  "type": "autogain",
  "phase": "testing",
  "gain": 7.7,
  "step": 3,
  "total_steps": 10,
  "results": []
}
```

**Complete:**

```json
{
  "type": "autogain",
  "phase": "done",
  "gain": 12.5,
  "step": 10,
  "total_steps": 10,
  "results": [
    { "gain": 0.0, "messages": 120 },
    { "gain": 0.9, "messages": 145 },
    { "gain": 1.4, "messages": 160 }
  ]
}
```

### Collector WebSocket — `/collector/ws` (server mode)

```
ws://<server-host>:8080/collector/ws
```

Used by remote collectors to push aircraft data to the central server. The collector initiates the connection and sends messages; the server receives them.

#### Handshake

The collector must send a `hello` message immediately after connecting:

```json
{
  "type": "hello",
  "collector_id": "dc-metro",
  "name": "DC Metro Receiver",
  "latitude": 38.856,
  "longitude": -77.050,
  "api_key": "my-secret-key"
}
```

The server validates the `api_key` (if configured) and registers the collector. If validation fails, the server closes the connection with code `4001`.

#### Collector Message Types

##### 1. `aircraft_update` — Forward aircraft state

```json
{
  "type": "aircraft_update",
  "collector_id": "dc-metro",
  "aircraft": {
    "icao": "A1B2C3",
    "callsign": "UAL123",
    "altitude": 35000,
    "latitude": 38.856,
    "longitude": -77.050
  }
}
```

The `aircraft` object follows the same schema as the `Aircraft` model. The server strips collector-local inferred fields (`distance_nm`, `bearing`) and recomputes them.

##### 2. `aircraft_remove` — Aircraft no longer seen

```json
{
  "type": "aircraft_remove",
  "collector_id": "dc-metro",
  "icao": "A1B2C3"
}
```

Indicates the collector no longer sees this aircraft. The server removes the collector from the aircraft's `source_collectors` list. The aircraft is only fully removed when no collectors report it and the stale timeout (60s) expires.

##### 3. `heartbeat` — Periodic stats

```json
{
  "type": "heartbeat",
  "collector_id": "dc-metro",
  "aircraft_count": 42,
  "messages_per_second": 13.4,
  "timestamp": 1708099200.0
}
```

Sent every 10 seconds. Used by the server to track collector health.

#### WebSocket Close Codes

| Code   | Reason               |
| ------ | -------------------- |
| `4000` | Expected hello       |
| `4001` | Invalid API key      |
| `4002` | Handshake timeout    |
| `4003` | Not in server mode   |

---

## Key Behaviors

| Behavior             | Detail                                                                                       |
| -------------------- | -------------------------------------------------------------------------------------------- |
| **Stale timeout**    | Aircraft are removed after 60 seconds with no messages                                       |
| **Enrichment**       | Metadata (registration, type, operator) is enriched from a local ADS-B Exchange DB (~615k aircraft), with hexdb.io as fallback |
| **DB auto-refresh**  | The aircraft metadata DB auto-refreshes when older than 7 days                               |
| **Units**            | Altitude: **feet** (barometric & geometric), Speed: **knots**, Track: **degrees** (0=N), Vertical rate: **ft/min**, Coordinates: **decimal degrees** |
| **ICAO codes**       | 24-bit hex addresses, uppercase (e.g. `A1B2C3`)                                             |
| **Multi-collector**  | In server mode, the same aircraft seen by multiple collectors is merged — most recent data wins, `source_collectors` tracks all reporting collectors |

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
  -d '{"latitude": 38.856, "longitude": -77.050, "gain": 12.5}'

# Trigger auto-gain
curl -X POST http://localhost:8080/api/autogain

# Check aircraft DB status
curl http://localhost:8080/api/db/status

# Force DB update
curl -X POST http://localhost:8080/api/db/update

# Get detected types
curl http://localhost:8080/api/types

# Get type summary
curl http://localhost:8080/api/types/summary

# List connected collectors (server mode)
curl http://localhost:8080/api/collectors

# Get a specific collector
curl http://localhost:8080/api/collectors/dc-metro
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
    case "autogain":
      handleAutogain(msg);
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

```python
import asyncio
import json
import websockets

async def collector():
    async with websockets.connect("ws://central-server:8080/collector/ws") as ws:
        # Send hello
        await ws.send(json.dumps({
            "type": "hello",
            "collector_id": "my-collector",
            "name": "My Receiver",
            "latitude": 38.856,
            "longitude": -77.050,
            "api_key": "my-secret-key",
        }))

        # Send aircraft updates
        await ws.send(json.dumps({
            "type": "aircraft_update",
            "collector_id": "my-collector",
            "aircraft": {
                "icao": "A1B2C3",
                "callsign": "UAL123",
                "altitude": 35000,
                "latitude": 38.856,
                "longitude": -77.050,
            }
        }))

asyncio.run(collector())
```
