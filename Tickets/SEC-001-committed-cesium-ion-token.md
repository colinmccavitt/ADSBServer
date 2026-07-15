# SEC-001 — Committed Cesium ion access token in source

- **Severity:** High
- **Area:** Security / Credentials
- **File:** [app/static/cesium-app.js:3](../app/static/cesium-app.js#L3)

## What's wrong

A Cesium ion access token (a JWT tied to ion account `id: 227455`) is hardcoded
and git-tracked:

```js
Cesium.Ion.defaultAccessToken = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...';
```

This is the account's **default** token — unscoped, so it grants access to
everything the ion account can do. It is already in git history.

Notably, commit `24b9593` ("Move API keys into secrets config and harden
auth/config handling") moved the *server-side* keys into the gitignored secrets
file but left this client-side credential behind.

## Why it matters

Cesium ion tokens are delivered to the browser by design, but a default,
unscoped token is a committed credential: anyone can view-source (or read the
public repo), lift it, and burn the account's tile/terrain quota or run up
billing against it. Because it's in history, deleting the line is not enough.

## Fix

1. **Rotate/revoke** the current token in the Cesium ion console now — it must
   be treated as compromised.
2. Issue a **scoped, restricted** token limited to the specific asset IDs the
   3D view uses (world terrain, OSM buildings) and, if ion supports it for your
   plan, an allowed-referrer/origin restriction.
3. Inject the token at runtime from server config (e.g. a field on
   `/api/config` or a templated value) instead of committing it, so rotation
   doesn't require a code change.
4. Optionally scrub it from history (e.g. `git filter-repo`) if the repo is or
   will be shared.
