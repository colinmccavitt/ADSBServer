# CFG-001 — `PUT /api/config` accepts out-of-range latitude/longitude

- **Severity:** Low
- **Area:** Input validation
- **File:** [app/main.py:34](../app/main.py#L34)–38 (`ServerConfig`),
  [app/main.py:227](../app/main.py#L227)–240 (`update_config`)

## What's wrong

The config update model accepts any float for latitude/longitude:

```python
class ServerConfig(BaseModel):
    latitude: float = Field(..., description="Server/receiver latitude")
    longitude: float = Field(..., description="Server/receiver longitude")
```

`update_config` writes them straight to `store.receiver_lat/lon` and persists
them to `config.json` with no range check. A value like `latitude=500` or
`longitude=9999` is accepted and saved.

## Why it matters

The receiver position feeds every distance/bearing calculation
(`_distance_bearing`) and the position-sanity gate in the decoder
(`MAX_DISTANCE_DEG` is measured relative to the collector, but the receiver
position drives the UI's range rings and per-aircraft distance). A garbage
receiver position silently corrupts all derived distance/bearing fields and the
map centering, and it's persisted so it survives a restart. It's an authed
endpoint, so this is robustness rather than an attack, but bad input should be
rejected rather than saved.

## Fix

Constrain the fields with pydantic:

```python
from pydantic import BaseModel, Field

class ServerConfig(BaseModel):
    latitude: float = Field(..., ge=-90, le=90, description="Server/receiver latitude")
    longitude: float = Field(..., ge=-180, le=180, description="Server/receiver longitude")
```

FastAPI will then return a 422 for out-of-range values before anything is
stored.
