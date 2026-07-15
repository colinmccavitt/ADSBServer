# TEST-001 — CollectorHub TCP server and its API-key gate have no tests

- **Severity:** Medium
- **Area:** Test coverage
- **File:** [app/collector_hub.py:160](../app/collector_hub.py#L160)–296 (`_handle_connection`)

## What's wrong

The full suite passes (`python -m pytest -q` → **67 passed**, one cosmetic
Starlette/httpx deprecation warning), and coverage of most surfaces is good —
including the `/ingest` WebSocket and, notably, a solid regression test for the
auth Referer/Origin substring-spoof (`test_auth.py`
`test_substring_spoofed_origin_is_rejected`).

But there is **no `test_collector_hub.py`**. The primary raw-hex ingest path —
the TCP server on port 4002 — is entirely untested:

- The API-key rejection at `collector_hub.py:182` (bad key →
  `{"error":"invalid_api_key"}` + close) has no test.
- Hello parsing, the 10s hello timeout, empty-hello handling, per-ICAO rate
  limiting (`MAX_UPDATES_PER_ICAO_PER_SEC`), and the single-bit CRC correction
  *integration* all run only in production.

Also untested: `app/aircraft_db.py`, `app/type_collector.py`, and the `/ws`
browser WebSocket, `/admin/stream` SSE generator, and `/api/db/*`,
`/api/types/*`, `/api/clients` routes.

Note: several `test_main.py` tests construct `TestClient(app)` **without** a
`with` block, which skips FastAPI lifespan — so the collector hub and DB never
start in those tests. Appropriate for HTTP-routing tests, but it structurally
excludes the lifespan-managed paths, reinforcing this gap.

## Why it matters

The most security-relevant server surface — the TCP collector authentication
gate — has zero automated protection against regressions. A refactor that breaks
the bad-key rejection would ship green.

## Fix

Add `test_collector_hub.py` driving `_handle_connection` over an in-memory
asyncio stream pair (`asyncio.StreamReader` + a fake writer, or
`asyncio.open_connection` against a hub bound to `127.0.0.1:0`). Assert:

- bad/missing key → `{"error":"invalid_api_key"}` written, connection closed,
  collector **not** registered;
- valid key → registration, and a decoded message reaches the store;
- empty hello and late hello (past the 10s timeout) are handled without crashing.
