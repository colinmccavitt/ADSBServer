# FE-002 — Collector numeric stats interpolated raw into innerHTML

- **Severity:** Low
- **Area:** Frontend / XSS
- **File:** [app/static/app.js:542](../app/static/app.js#L542)–543,
  [app/static/admin.js:97](../app/static/admin.js#L97)–98

## What's wrong

Two server-provided collector stats are interpolated directly into `innerHTML`
without escaping:

```js
// app.js
<div>${c.aircraft_count} aircraft</div>
<div>${c.messages_per_second} msg/s</div>
// admin.js
<td>${c.aircraft_count}</td>
<td>${c.messages_per_second}</td>
```

Every *string* field (callsign, registration, owner_operator, collector name,
`remote_addr`, aircraft_model, client_id) is correctly run through `escHtml()`
or assigned via `textContent` — this was verified across all `innerHTML` sinks.
These two numeric fields are the only server values interpolated raw.

## Why it matters

These originate from collector-reported activity. If the backend ever passes
them through without coercing to a number (they flow through
`CollectorInfo.aircraft_count: int` / `messages_per_second: float` today, so
pydantic currently coerces them — this is defense-in-depth), a value containing
markup would become stored XSS in the admin dashboard and the 2D map. Low
confidence, but it's the one raw-interpolation gap in otherwise well-defended
client code.

## Fix

Coerce or escape at the render site:

```js
<div>${Number(c.aircraft_count) || 0} aircraft</div>
<div>${Number(c.messages_per_second) || 0} msg/s</div>
```

(or route them through `escHtml`). Cheap, and removes the dependency on the
server's typing staying correct forever.
