# FE-003 — `ws.onmessage` JSON parse is unguarded

- **Severity:** Low
- **Area:** Frontend / robustness
- **File:** [app/static/app.js:407](../app/static/app.js#L407),
  [app/static/cesium-app.js:667](../app/static/cesium-app.js#L667)

## What's wrong

Both WebSocket clients parse incoming frames with no error handling:

```js
ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);   // no try/catch
    ...
};
```

A single malformed frame throws inside the handler and that message is dropped
with an uncaught error. `updateStats` (app.js:392) similarly calls
`stats.messages_total.toLocaleString()` with no guard, throwing if that field is
ever absent.

This is inconsistent with `admin.js:153-161`, which *does* wrap its SSE parse in
try/catch.

## Why it matters

Reconnect is tied to `onclose`, so a bad frame does not kill the socket — but it
does silently lose that update and spam the console. It's a robustness gap, not
a crash. Worth aligning with the admin.js pattern.

## Fix

Wrap the parse/dispatch body in try/catch and log, mirroring `admin.js`:

```js
ws.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); }
    catch (e) { console.warn("bad WS frame", e); return; }
    ...
};
```

## Note (not a defect) — verified-good client behavior

For the record, the audit confirmed these are handled correctly and need no
ticket: WS/SSE reconnect (3s backoff via `onclose`, `onerror` funnels into it),
`remove`-message cleanup of markers/labels/trails, client-side stale prune
(5s/70s safety net), bounded trail arrays (100 / 200 points), and try/catch on
every `fetch`. One optional hardening: `cesium.html` loads Cesium from the CDN
without an SRI `integrity` attribute, whereas `index.html` correctly pins
Leaflet with SRI — adding SRI to the Cesium tags would close a supply-chain gap.
