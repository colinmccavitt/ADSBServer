# PRIV-001 — Admin dashboard, SSE stream, and watchlist are unauthenticated

- **Severity:** Medium
- **Area:** Privacy / Information disclosure
- **File:** [app/main.py:133](../app/main.py#L133)–183 (`/admin`, `/admin/stream`),
  [app/main.py:247](../app/main.py#L247)–250 (`/api/watchlist`)

## What's wrong

Several endpoints carry no `Depends(auth.require_api_key)` and are reachable by
anyone who can hit the HTTP port:

- **`GET /admin`** and **`GET /admin/stream`** — the SSE stream emits, every 2
  seconds, the full collector list (including each collector's `latitude` /
  `longitude`), the connected client list (including each client's
  `remote_addr` IP), and receiver stats. The code comment even says
  "no auth required — admin page is open".
- **`GET /api/watchlist`** — returns the entire tail-number watchlist from
  `config.json` (currently ~96 specific N-numbers).

## Why it matters

This discloses operational data with no authentication:

- Collector coordinates reveal the physical locations of the receiver sites.
- `remote_addr` exposes the IP addresses of every connected browser/client.
- The watchlist is a curated list of specific aircraft registrations the
  operator is tracking. Publishing it unauthenticated leaks *who/what is being
  watched* to anyone who requests the URL.

Even if the map UI itself is intended to be public, the admin telemetry and the
watchlist are a different sensitivity class than "here are planes near me".

## Fix

- Put `/admin` and `/admin/stream` behind `require_api_key` (or a dedicated
  admin credential). The dashboard should authenticate before streaming
  collector/client internals.
- Decide explicitly whether `/api/watchlist` is public. If the browser UI needs
  it, serve it through the same authenticated path as the rest of `/api/*`
  (see AUTH-001 for how the UI should authenticate) rather than leaving a
  standalone unauthenticated route.
- If any of these are genuinely meant to be public, say so in `API.md` and in a
  code comment, so it's a documented decision rather than an omission.
