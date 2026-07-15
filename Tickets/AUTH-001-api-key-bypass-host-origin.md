# AUTH-001 — API-key check bypassable via spoofed Host/Origin headers

- **Severity:** High
- **Area:** Authentication
- **File:** [app/auth.py:88](../app/auth.py#L88)–147

## What's wrong

`require_api_key` grants access without an API key whenever the request's
`Referer` or `Origin` header has the same host as the request's own `Host`
header (the "browser UI exemption"):

```python
server_host = request.headers.get("host", "")
if server_host:
    for header_val in (referer, origin):
        if header_val and _same_host(header_val, server_host):
            return   # <-- passes with no X-API-Key
```

All three values — `Host`, `Origin`, and `Referer` — are fully controlled by
the client on any non-browser request. An attacker using `curl`, a script, or
any HTTP library simply sets them to match:

```
curl -H "Host: target:8080" -H "Origin: http://target:8080" \
     http://target:8080/api/aircraft
```

`_same_host` parses the Origin netloc (`target:8080`) and compares it to the
Host header (`target:8080`) — they match, so the request is authorized. The
`X-API-Key` requirement is bypassed entirely.

## Why it matters

The browser-origin exemption exists to let the first-party web UI call `/api/*`
without a key. But the exemption is exactly as strong as the attacker's ability
to set two request headers — i.e. not strong at all for the threat this control
is meant to stop (scripted/non-browser clients pulling the aircraft feed, stats,
collector list, and forcing DB downloads). Browsers forbid scripts from setting
`Host`/`Origin`, but a direct attacker is under no such restriction, so every
`/api/*` endpoint behind `require_api_key` is readable/callable without a key.

## Fix

The Host header cannot be a trust anchor because the client sets it. Options,
best first:

1. **Drop the Referer/Origin exemption entirely.** Have the browser UI
   authenticate like any other client — serve it a short-lived session cookie
   or a scoped key on page load, and require that on `/api/*`. This removes the
   header-based trust path completely.
2. If a same-origin exemption must stay, gate it on something the client can't
   forge: a `Sec-Fetch-Site: same-origin` check *combined with* a
   CSRF/session token, and compare Origin against the server's **configured**
   public origin (from config, not the request's Host header) rather than
   against `request.headers["host"]`.

Also add a regression test asserting that a request with matching spoofed
`Host`+`Origin` and no `X-API-Key` is rejected with 401.
