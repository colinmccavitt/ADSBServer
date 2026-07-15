# API-001 — Not-found / uninitialized responses return HTTP 200

- **Severity:** Low
- **Area:** API contract
- **File:** [app/main.py:197](../app/main.py#L197)–203 (`get_aircraft`),
  [app/main.py:298](../app/main.py#L298)–306 (`get_collector`)

## What's wrong

Error conditions are signalled with a 200 status and an `{"error": ...}` body
instead of an appropriate HTTP status:

```python
@app.get("/api/aircraft/{icao}", ...)
async def get_aircraft(icao: str):
    ac = await store.get_by_icao(icao)
    if ac is None:
        return {"error": f"Aircraft {icao.upper()} not found"}   # HTTP 200
    return ac

@app.get("/api/collectors/{collector_id}", ...)
async def get_collector(collector_id: str):
    if collector_hub is None:
        return {"error": "Collector hub not initialized"}        # HTTP 200
    info = collector_hub.get_collector(collector_id)
    if info is None:
        return {"error": f"Collector {collector_id} not found"}  # HTTP 200
```

## Why it matters

Clients can't rely on the status code to detect failures — a "not found" looks
like a success, so callers must special-case the response body. It also breaks
the OpenAPI contract (the documented response model is the aircraft object, not
an error shape) and any generic retry/error middleware.

## Fix

Raise `HTTPException` with the right status:

```python
from fastapi import HTTPException

if ac is None:
    raise HTTPException(status_code=404, detail=f"Aircraft {icao.upper()} not found")
...
if collector_hub is None:
    raise HTTPException(status_code=503, detail="Collector hub not initialized")
if info is None:
    raise HTTPException(status_code=404, detail=f"Collector {collector_id} not found")
```
