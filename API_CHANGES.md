# ADS-B Server — API Changes (v3.1)

This document describes the breaking and non-breaking changes introduced in the latest server update. If you maintain a client, collector, or integration, read the relevant sections below.

---

## Summary of Changes

1. **API key authentication** is now enforced on all `/api/*` REST endpoints
2. **Collector authentication** is now enforced on TCP connections (port 4002)
3. **New admin dashboard** at `/admin` with live monitoring
4. **New endpoints** added: `/api/clients`, `/admin/stream`
5. **Browser access is unaffected** — the web UI continues to work without keys

---

## Breaking Change: API Key Required for REST Endpoints

All `/api/*` endpoints now require authentication for programmatic (non-browser) access.

### How to authenticate

Pass your client API key in the `X-API-Key` HTTP header:

```bash
curl -H "X-API-Key: YOUR_CLIENT_KEY" http://<host>:8080/api/aircraft
```

### Affected endpoints

Every endpoint under `/api/` is affected:

| Endpoint                       | Method |
| ------------------------------ | ------ |
| `/api/aircraft`                | GET    |
| `/api/aircraft/{icao}`         | GET    |
| `/api/stats`                   | GET    |
| `/api/config`                  | GET    |
| `/api/config`                  | PUT    |
| `/api/db/status`               | GET    |
| `/api/db/update`               | POST   |
| `/api/types`                   | GET    |
| `/api/types/summary`           | GET    |
| `/api/collectors`              | GET    |
| `/api/collectors/{id}`         | GET    |
| `/api/clients` *(new)*         | GET    |

### Error response

Requests without a valid key receive:

```
HTTP 401 Unauthorized
```

```json
{
  "detail": "Valid API key required. Pass via X-API-Key header."
}
```

### Browser exemption

Requests originating from the server's own web UI (detected via the `Referer` or `Origin` header matching the server host) are allowed without a key. This means the browser-based map and 3D views continue to work seamlessly. Only external/programmatic clients (curl, scripts, CLI tools) need a key.

### Getting a client API key

Client API keys are configured in the server's `config.json` under `api_keys.client_keys`. Contact the server administrator for a key.

### Code migration examples

**Python (httpx / requests):**

```python
import httpx

API_KEY = "your-client-key-here"

resp = httpx.get(
    "http://server:8080/api/aircraft",
    headers={"X-API-Key": API_KEY},
)
data = resp.json()
```

**JavaScript (fetch):**

```javascript
const API_KEY = "your-client-key-here";

const res = await fetch("http://server:8080/api/aircraft", {
    headers: { "X-API-Key": API_KEY },
});
const data = await res.json();
```

**curl:**

```bash
curl -H "X-API-Key: your-client-key-here" http://server:8080/api/aircraft
```

---

## Breaking Change: Collector API Key Required

Collectors connecting via TCP on port 4002 must now include an `api_key` field in their JSON hello message.

### Before (no longer accepted when keys are configured)

```json
{"id": "my-collector", "name": "Roof Antenna", "lat": 38.85, "lon": -77.04}
```

### After

```json
{"id": "my-collector", "name": "Roof Antenna", "lat": 38.85, "lon": -77.04, "api_key": "your-collector-key-here"}
```

### Rejection behavior

If the key is missing or invalid, the server responds with:

```json
{"error": "invalid_api_key"}
```

...and immediately closes the TCP connection.

### Getting a collector API key

Collector API keys are configured in the server's `config.json` under `api_keys.collector_keys`. Contact the server administrator for a key.

---

## New Endpoint: `GET /api/clients`

Returns a list of all currently connected WebSocket clients.

**Requires:** `X-API-Key` header (same as other `/api/*` endpoints).

### Response

```json
[
  {
    "client_id": "a1b2c3d4e5f6",
    "client_type": "browser",
    "remote_addr": "192.168.1.10:54321",
    "connected_since": "2026-02-17T14:02:43.407543"
  }
]
```

| Field             | Type             | Description                              |
| ----------------- | ---------------- | ---------------------------------------- |
| `client_id`       | `string`         | Unique connection identifier             |
| `client_type`     | `string`         | `"browser"` or `"api"`                   |
| `remote_addr`     | `string`         | Remote IP and port                       |
| `connected_since` | `datetime` (ISO) | When the client connected                |

---

## New: Admin Dashboard

A live monitoring dashboard is now available at:

```
http://<host>:8080/admin
```

**No authentication required.** The admin page shows:

- **Server stats** — aircraft count, message rate, uptime
- **Active collectors** — name, location, aircraft count, message rate, health status
- **Connected clients** — type (browser/API), remote address, connection duration

The page updates automatically every 2 seconds via Server-Sent Events.

### SSE Endpoint: `GET /admin/stream`

If you want to build your own monitoring integration, you can consume the SSE stream directly:

```bash
curl -N http://server:8080/admin/stream
```

Each event contains:

```json
{
  "collectors": [ ... ],
  "clients": [ ... ],
  "stats": {
    "uptime_seconds": 3600.5,
    "aircraft_count": 12,
    "aircraft_with_position": 9,
    "messages_total": 48230,
    "messages_per_second": 13.4,
    "collector_count": 2,
    "client_count": 3
  }
}
```

---

## Unchanged

The following are **not affected** by these changes:

- **WebSocket `/ws`** — browser WebSocket connections still work without a key
- **HTML pages** — `/`, `/3d`, `/admin` are served without authentication
- **Static assets** — `/static/*` files are served without authentication
- **Data models** — the `Aircraft`, `AircraftList`, `ReceiverStats`, and `CollectorInfo` response schemas are unchanged
- **WebSocket message protocol** — `update`, `remove`, and `autogain` messages are unchanged

---

## Configuration Reference

The `api_keys` section in `config.json`:

```json
{
  "api_keys": {
    "collector_keys": ["key-1", "key-2"],
    "client_keys": ["key-a", "key-b"]
  }
}
```

| Field            | Type       | Description                                      |
| ---------------- | ---------- | ------------------------------------------------ |
| `collector_keys` | `string[]` | Valid API keys for TCP collector connections      |
| `client_keys`    | `string[]` | Valid API keys for REST API access (CLI/scripts)  |

**Backward compatibility:** If the `api_keys` section is omitted or both arrays are empty, all authentication is disabled and the server behaves as before.
