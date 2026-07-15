# FE-001 — Disconnected collectors leave ghost markers; empty case never clears

- **Severity:** Medium
- **Area:** Frontend (2D map)
- **File:** [app/static/app.js:510](../app/static/app.js#L510)–570 (`loadCollectors`),
  [app/static/app.js:572](../app/static/app.js#L572)–597 (`updateCollectorMarker`)

## What's wrong

`loadCollectors()` polls `/api/collectors` every 10s but only ever **adds or
updates** markers — nothing removes a marker for a collector that has since
disconnected:

```js
if (!Array.isArray(collectors) || collectors.length === 0) {
    return;   // <-- early return BEFORE clearing list/markers
}
...
if (collectorMarkers[id]) {
    collectorMarkers[id].setLatLng([lat, lon]);
} else {
    collectorMarkers[id] = L.marker(...).addTo(map);   // only ever grows
}
```

Two concrete problems:

1. A collector that drops off leaves its amber marker on the map permanently;
   `collectorMarkers` grows monotonically (ghost receivers).
2. When the list goes from non-empty back to **empty**, the early `return` at
   the top fires *before* `collectors-list` is cleared or any marker removed —
   so both the stale sidebar rows and the stale markers persist.

## Why it matters

Ghost receiver markers misrepresent which collectors are actually online, and
the marker set never shrinks for the life of the page.

## Fix

After fetching, diff the returned `collector_id` set against `collectorMarkers`
and remove the stragglers; drop the early return so the empty case also clears:

```js
const seen = new Set(collectors.map(c => c.collector_id));
for (const id of Object.keys(collectorMarkers)) {
    if (!seen.has(id)) {
        map.removeLayer(collectorMarkers[id]);
        delete collectorMarkers[id];
    }
}
// clear + rebuild the sidebar list unconditionally (including the empty case)
```
