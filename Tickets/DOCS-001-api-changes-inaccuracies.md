# DOCS-001 — `API_CHANGES.md` inaccuracies (missing `/ingest`, overstated auth)

- **Severity:** Low
- **Area:** Documentation
- **File:** `API_CHANGES.md`

## What's wrong

`API.md` is accurate — every route in `app/main.py` is documented, and the auth
annotations match the code (watchlist/admin/`/ws` flagged keyless; the rest of
`/api/*` require `X-API-Key`). `API_CHANGES.md`, however, has three gaps:

1. **`/ingest` is undocumented** (the "Unchanged" section lists only `/ws`). The
   structured-push WebSocket ingest endpoint was added after this doc and never
   recorded here — a collector author reading only `API_CHANGES.md` would miss
   it. (API.md does cover it.)
2. **Overstated auth scope** — the prose says "Every endpoint under `/api/` is
   affected." That's false: `/api/watchlist` has no auth dependency. The table
   just below correctly omits watchlist, so the prose contradicts its own table.
3. **No note that `/admin/stream` leaks data unauthenticated.** The SSE feed
   returns `clients[].remote_addr` (client IP:port) and collector locations with
   no auth; the docs present this as a feature without flagging the exposure
   (see PRIV-001).

## Why it matters

`API_CHANGES.md` is the migration/reference doc for integrators. The auth
overstatement in particular could lead someone to assume watchlist is protected
when it isn't.

## Fix

- Add an `/ingest` section describing the hello handshake and frame shapes.
- Soften the auth claim to "every `/api/` endpoint **except `/api/watchlist`**."
- Add a security note that `/admin` and `/admin/stream` expose client/collector
  metadata without authentication (and cross-reference the fix in PRIV-001).
