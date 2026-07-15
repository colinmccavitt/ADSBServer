# PERF-002 — Unbounded hexdb.io lookup cache grows forever

- **Severity:** Low
- **Area:** Performance / Memory
- **File:** [app/aircraft_db.py:37](../app/aircraft_db.py#L37),
  [app/aircraft_db.py:81](../app/aircraft_db.py#L81)–98

## What's wrong

`AircraftDB._api_cache` caches every hexdb.io lookup — including negative
results — with no size bound and no eviction:

```python
self._api_cache: dict[str, dict[str, Any] | None] = {}
...
if icao_upper in self._api_cache:
    return self._api_cache[icao_upper]
result = await self._fetch_hexdb(icao_upper)
self._api_cache[icao_upper] = result   # never evicted
```

Every distinct ICAO that isn't in the local DB adds a permanent entry.

## Why it matters

On a long-running server that sees a lot of traffic (or aircraft not in the
local snapshot), the cache grows monotonically for the life of the process.
Each entry is small, so this is slow-burn rather than acute, but it's an
unbounded structure with no ceiling — a genuine leak over weeks of uptime.

## Fix

Bound it. Simplest is an LRU:

```python
from functools import lru_cache  # (sync) — or a small manual OrderedDict LRU for async

# Or, keeping the async method, use a capped OrderedDict:
if len(self._api_cache) >= MAX_API_CACHE:
    self._api_cache.popitem(last=False)  # evict oldest
```

Alternatively cache negative results with a short TTL and positive results with
a longer one, so unknown ICAOs don't pin memory permanently. A cap of a few
thousand entries is plenty.
