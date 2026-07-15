# PERF-001 — Multi-MB gzip/JSON decode blocks the event loop

- **Severity:** Medium
- **Area:** Performance / Availability
- **File:** [app/aircraft_db.py:104](../app/aircraft_db.py#L104)–130 (`refresh`),
  [app/aircraft_db.py:181](../app/aircraft_db.py#L181)–198 (`_count_entries`),
  [app/aircraft_db.py:203](../app/aircraft_db.py#L203)–258 (`_load_from_disk`)

## What's wrong

`POST /api/db/update` is an `async` handler that calls `aircraft_db.refresh()`,
which does the network download with `httpx` (fine, non-blocking) and then runs
**CPU-bound, synchronous** work directly on the event loop:

- `_count_entries()` → `gzip.decompress()` + `json.loads()` on the freshly
  downloaded multi-megabyte payload.
- `_load_from_disk()` → `gzip.open().read()` + `json.loads()` of the whole DB,
  building a dict of every aircraft entry.

The same synchronous `_load_from_disk()` also runs from the scheduled refresh
loop and the background staleness refresh.

## Why it matters

`gzip.decompress` + `json.loads` on a DB of this size takes hundreds of
milliseconds to seconds. Because it runs on the single asyncio thread, **every**
WebSocket broadcast, SSE tick, collector TCP read, and HTTP request is frozen
for that entire window. One authenticated `/api/db/update` call (or a scheduled
refresh) stalls all live clients and can drop collector connections.

## Fix

Offload the blocking decode/parse to a thread so the loop keeps running:

```python
await asyncio.to_thread(self._load_from_disk)
count = await asyncio.to_thread(self._count_entries, resp.content)
```

Apply to all three call sites (`refresh`, `_background_download_and_reload`, and
the initial load if it ever runs after startup). The startup-time load in
`start()` is less critical since no clients are connected yet, but routing it
through `to_thread` too keeps it consistent.
