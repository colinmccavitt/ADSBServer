# AUTH-002 — Auth silently disabled (open access) when no keys configured

- **Severity:** Medium
- **Area:** Authentication
- **File:** [app/auth.py:52](../app/auth.py#L52)–78, [app/auth.py:103](../app/auth.py#L103)–147
- **Related:** `config.secrets.example.json`, [app/collector_hub.py:82](../app/collector_hub.py#L82)

## What's wrong

Both auth gates fail *open*. When no keys are configured they accept everything:

```python
def validate_collector_key(key):
    ...
    if not _collector_keys:
        return True  # no keys configured — open access

async def require_api_key(...):
    ...
    if not _client_keys:
        return       # open access (backward compatible)
```

`config.secrets.example.json` ships with **empty** `collector_keys` and
`client_keys` arrays, and `load_secrets()` returns `{}` when the file is
missing. So the default state of a fresh deployment is:

- Any client can pull the full aircraft feed, stats, and collector list.
- Any TCP client that connects to the collector hub (bound to `0.0.0.0`,
  collector_hub.py:82) can inject arbitrary decoded aircraft into the store.

There is no startup warning that the server is running unauthenticated.

## Why it matters

"Fail open" means the security control is absent precisely when an operator
forgets to configure it — the most common misconfiguration. Combined with the
collector hub listening on all interfaces, an unconfigured server on a routable
network accepts spoofed aircraft data from anyone. The `assert _collector_keys
is not None` guards are also load-bearing correctness assumptions that would
raise if `-O` (optimized) Python ever stripped asserts.

## Fix

- At minimum, log a prominent `WARNING` at startup when either key set is empty
  ("collector/client authentication is DISABLED — no keys configured").
- Preferably, make auth **fail closed** for the collector hub when it binds to a
  non-loopback interface: refuse collector connections unless at least one
  collector key exists, or bind to `127.0.0.1` by default and require explicit
  opt-in to `0.0.0.0`.
- Replace the `assert ... is not None` guards with explicit `_ensure_loaded()`
  guarantees that don't depend on `assert` (which is a no-op under `python -O`).
