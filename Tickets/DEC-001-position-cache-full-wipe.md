# DEC-001 — Decoder position cache wipes all references at 1000 entries

- **Status:** RESOLVED 2026-07-14 — `_last_pos` is now an `OrderedDict` LRU
  bounded by `MAX_POSITION_REFS` with per-entry eviction (oldest only, never
  a bulk clear). Covered by `test_position_ref_cache_evicts_oldest_only`.
- **Severity:** Low
- **Area:** Correctness
- **File:** [app/decoder.py:332](../app/decoder.py#L332)–333

## What's wrong

The reference-based CPR decoder keeps a per-aircraft last-known position in
`_last_pos`. When it exceeds 1000 entries it clears the **entire** dict:

```python
if len(self._last_pos) > 1000:
    self._last_pos.clear()
```

## Why it matters

`_last_pos` is the reference point for single-frame CPR position decoding
(`airborne_position_with_ref` / `surface_position_with_ref`). Clearing all of it
at once means every currently-tracked aircraft momentarily loses its position
reference and falls back to the collector's location as the reference — degrading
or dropping position decodes for a beat until each aircraft re-establishes its
own reference. It's a periodic, correlated glitch across all contacts rather
than a graceful per-aircraft expiry.

Entries are also never expired individually, so a departed aircraft's stale
position lingers until the next full wipe.

## Fix

Use a bounded LRU or per-entry TTL instead of a bulk clear. For example an
`OrderedDict` that evicts the oldest single entry when full:

```python
from collections import OrderedDict
self._last_pos: OrderedDict[str, tuple[float, float]] = OrderedDict()
...
self._last_pos[icao] = (lat, lon)
self._last_pos.move_to_end(icao)
if len(self._last_pos) > 1000:
    self._last_pos.popitem(last=False)   # evict only the oldest
```

This keeps active aircraft's references intact and only sheds the least-recently
seen.
