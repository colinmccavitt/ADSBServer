# STORE-002 — ICAO not upper-cased on `/ingest`, breaking lookups and dedup

- **Severity:** Medium
- **Area:** Correctness
- **File:** [app/aircraft_store.py:185](../app/aircraft_store.py#L185) (`update`),
  [app/aircraft_store.py:788](../app/aircraft_store.py#L788)–791 (`get_by_icao`),
  [app/main.py:371](../app/main.py#L371)–378 (`/ingest` path)

## What's wrong

`AircraftStore.update()` uses the incoming ICAO verbatim as the dict key — it
never upper-cases it:

```python
icao = data.pop("icao")   # stored as-is
...
self._aircraft[icao] = ...
```

The raw-hex decoder path always upper-cases ICAOs before calling `update`
(`decoder.py` does `icao.upper()`), so that path is internally consistent. But
the **`/ingest` WebSocket path** stores whatever the preprocessing collector
sends — if the collector emits lowercase hex (`"a7d39d"`), the aircraft is keyed
lowercase.

Meanwhile the read path upper-cases:

```python
async def get_by_icao(self, icao):
    return self._aircraft.get(icao.upper())
```

## Why it matters

For a preprocessed collector that sends lowercase ICAOs:

- **`GET /api/aircraft/{icao}` always misses** — the lookup upper-cases, the
  stored key is lowercase, so a tracked aircraft returns "not found".
- **Multi-source dedup splits.** If the same aircraft arrives via the hex path
  (uppercase key) and the ingest path (lowercase key), it's stored as two
  separate contacts with two markers, and `_source_collectors` tracking is
  fragmented across the two keys.

## Fix

Normalize once, at the boundary, in `update()`:

```python
icao = data.pop("icao").upper()
```

That makes every storage path consistent with the uppercase read path and with
the decoder. Add a test that ingests a lowercase-ICAO frame and asserts
`get_by_icao` (upper) and `/api/aircraft/{icao}` both find it.
