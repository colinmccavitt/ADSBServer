# STORE-001 — `x or y` truthiness bugs corrupt 0.0 coordinates and due-north track

- **Status: RESOLVED (2026-07-14) — by deletion, not patch.** The
  ghost-suppression high-water mark, the `smoothed_*` EMA/dead-reckon
  smoother, and the `smoothed_*` schema fields described below were removed
  entirely as part of a project-wide move to raw, unfiltered live ADS-B data
  (WOPR5000 + ADSBServer + ADSBCollector all now pass through raw kinematics
  with no smoothing). The buggy `x or y` fallbacks no longer exist because the
  code that contained them no longer exists. Kept here for history.
- **Severity:** Medium
- **Area:** Correctness
- **File (historical, pre-deletion):** `app/aircraft_store.py`

## What's wrong

Several places pick a "preferred, else fallback" value with `or`, which treats a
valid `0.0` as missing:

```python
# _is_position_behind
new_lat = ac_dict.get("smoothed_latitude") or ac_dict.get("latitude")
new_lon = ac_dict.get("smoothed_longitude") or ac_dict.get("longitude")

# _update_broadcast_high_water
lat   = ac_dict.get("smoothed_latitude") or ac_dict.get("latitude")
lon   = ac_dict.get("smoothed_longitude") or ac_dict.get("longitude")
track = ac_dict.get("smoothed_track")     or ac_dict.get("track")

# _compute_smoothed
s_track_prev = prev.get("s_track") or prev.get("track")
s_speed_prev = prev.get("s_speed") or prev.get("ground_speed")
```

Any legitimately-zero value silently falls through to the alternative:

- **`track` / `smoothed_track` of `0.0`** = due north — an extremely common
  heading. `smoothed_track or track` discards the smoothed value; in
  `_update_broadcast_high_water` a smoothed track of exactly 0° falls back to
  the raw track, and in `_compute_smoothed` a previous smoothed track of 0°
  falls back to raw, defeating the EMA at due north.
- **`latitude`/`longitude` of `0.0`** — the equator and prime meridian. Less
  common for this receiver, but a smoothed lat of exactly 0.0 would be dropped.
- **`ground_speed` / `s_speed` of `0.0`** — a stationary aircraft; the smoothed
  0 kt falls back to raw.

## Why it matters

Ghost-suppression (`_is_position_behind`), the broadcast high-water mark, and
the EMA smoother all depend on these values being the *smoothed* ones. At due
north (track ≈ 0°) the high-water track becomes the raw jittery track and the
smoother reseeds from raw, reintroducing exactly the bounce/jitter this code was
written to remove.

## Fix

Use an explicit `None` check instead of truthiness. A small helper keeps it
readable:

```python
def _prefer(a, b):
    return a if a is not None else b

new_lat = _prefer(ac_dict.get("smoothed_latitude"), ac_dict.get("latitude"))
```

Apply at all four sites (and audit for other `... or ...` fallbacks over
numeric fields). Add a regression test with `track=0.0` asserting the smoothed
value is preserved through a broadcast cycle.
