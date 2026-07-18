# AUTH-001 — API-key check bypassable via spoofed Host/Origin headers

- **Status:** RESOLVED (2026-07-18)
- **Severity:** High
- **Area:** Authentication
- **File:** [app/auth.py](../app/auth.py)

## Resolution

The Referer/Origin/Host exemption was **removed**. Browser UI auth now uses
an HttpOnly `SameSite=Strict` cookie (`adsb_api_key`) stamped when serving
`/`, `/3d`, and `/admin`. The cookie value is a real configured `client_key`.
`require_api_key` accepts only:

1. A valid `X-API-Key` header, or
2. A valid `adsb_api_key` cookie,

…when `client_keys` is non-empty. Spoofing `Host`+`Origin` alone yields 401.
`/ws` uses the same rules via `accept_websocket_if_authorized`.

Regression: `tests/test_auth.py::test_origin_referer_exemption_removed`.

## Original issue

`require_api_key` granted access without an API key whenever the request's
`Referer` or `Origin` header had the same host as the request's own `Host`
header. All three values are client-controlled on non-browser requests, so
any `curl` with matching Host+Origin bypassed the key check.
