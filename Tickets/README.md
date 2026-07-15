# ADS-B Server — Audit Tickets

Findings from a repository audit on 2026-07-14. One file per issue. Severity is
the audit's own assessment (Critical / High / Medium / Low), weighing both impact
and how reachable the problem is in a normal deployment.

Each ticket lists the affected file/line, what's wrong, why it matters, and a
concrete fix. Line numbers reflect the working tree at audit time.

## Priority order

The two credential findings (SEC-002, SEC-001) and the auth bypass (AUTH-001)
should be handled first — rotate keys before anything else.

| ID | Severity | Area | Title |
|----|----------|------|-------|
| [SEC-002](SEC-002-api-keys-in-git-history.md) | **Critical** | Security | Live API keys (3 collector + 2 client) recoverable from git history |
| [SEC-001](SEC-001-committed-cesium-ion-token.md) | High | Security | Committed Cesium ion access token in source |
| [AUTH-001](AUTH-001-api-key-bypass-host-origin.md) | High | Auth | API-key check bypassable via spoofed Host/Origin headers |
| [AUTH-002](AUTH-002-open-by-default.md) | Medium | Auth | Auth silently disabled (open access) when no keys configured |
| [PRIV-001](PRIV-001-unauthenticated-admin-and-watchlist.md) | Medium | Privacy | Admin dashboard, SSE stream, and watchlist are unauthenticated |
| [DEPLOY-001](DEPLOY-001-default-bind-all-interfaces.md) | Medium | Deployment | Server binds all interfaces by default, exposing unauth surfaces |
| [PERF-001](PERF-001-blocking-db-load-on-event-loop.md) | Medium | Performance | Multi-MB gzip/JSON decode blocks the event loop |
| [STORE-001](STORE-001-zero-falsy-position-track.md) | Medium | Correctness | ~~`x or y` truthiness bugs corrupt 0.0 lat/lon and due-north track~~ — **RESOLVED**: the smoothing/ghost-suppression code was deleted, not patched |
| [STORE-002](STORE-002-icao-case-mismatch-ingest.md) | Medium | Correctness | ICAO not upper-cased on `/ingest`, breaks lookups and dedup |
| [FE-001](FE-001-collector-markers-never-removed.md) | Medium | Frontend | Disconnected collectors leave ghost markers; empty case never clears |
| [TEST-001](TEST-001-collector-hub-untested.md) | Medium | Testing | CollectorHub TCP server and its API-key gate have no tests |
| [PKG-001](PKG-001-unpinned-and-missing-deps.md) | Medium | Packaging | Dependencies unpinned; test dependencies missing |
| [API-001](API-001-error-responses-return-200.md) | Low | API | Not-found / uninitialized responses return HTTP 200 |
| [PERF-002](PERF-002-unbounded-hexdb-cache.md) | Low | Performance | Unbounded hexdb.io lookup cache grows forever |
| [DEC-001](DEC-001-position-cache-full-wipe.md) | Low | Correctness | Decoder position cache wipes all references at 1000 entries |
| [CFG-001](CFG-001-no-latlon-validation.md) | Low | Validation | `PUT /api/config` accepts out-of-range latitude/longitude |
| [FE-002](FE-002-unescaped-numeric-fields-xss.md) | Low | Frontend | Collector numeric stats interpolated raw into innerHTML |
| [FE-003](FE-003-ws-onmessage-no-guard.md) | Low | Frontend | `ws.onmessage` JSON parse is unguarded |
| [DOCS-001](DOCS-001-api-changes-inaccuracies.md) | Low | Docs | `API_CHANGES.md` inaccuracies (missing `/ingest`, overstated auth) |

## Verified clean (no ticket)

- **.gitignore hygiene** — nothing `.gitignore` excludes is tracked;
  `config.secrets.json` was never committed.
- **Test quality** — existing tests assert real behavior (real pyModeS
  vectors, CRC bit-flip recovery, raw-passthrough kinematics), not
  tautologies.
- **Client reconnect / cleanup** — WS & SSE reconnect with backoff, `remove`
  frames clean up markers/labels/trails, client-side stale prune, bounded trail
  arrays, try/catch on every `fetch`.
- **String-field XSS** — all attacker-influenceable string fields are escaped
  via `escHtml()` / `textContent` (the only raw interpolations are the numeric
  fields in FE-002).
- **API.md route/auth accuracy** — matches `app/main.py` (the doc issues are in
  `API_CHANGES.md`, DOCS-001).
