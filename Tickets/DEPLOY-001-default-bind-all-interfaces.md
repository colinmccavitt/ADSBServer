# DEPLOY-001 — Server binds all interfaces by default, exposing unauthenticated surfaces

- **Severity:** Medium
- **Area:** Deployment / Security
- **File:** [start.py:36](../start.py#L36)–47, [start.py:51](../start.py#L51)–59,
  [app/collector_hub.py:82](../app/collector_hub.py#L82)–84

## What's wrong

`start.py` defaults the HTTP bind address to `0.0.0.0`:

```python
parser.add_argument("--host", default="0.0.0.0", help="HTTP bind address (default: 0.0.0.0)")
```

and the collector hub always binds `0.0.0.0` (`asyncio.start_server(..., "0.0.0.0", port)`).

So out of the box the server publishes to the entire network/internet:

- The **unauthenticated** `/admin` dashboard and `/admin/stream` SSE, which leak
  connected-client IPs and collector positions (see PRIV-001).
- The API in **open-access mode** when no client keys are configured (see
  AUTH-002).
- The collector TCP port, which accepts injected aircraft when no collector keys
  are configured.

Secondary nit: the startup banner prints `http://0.0.0.0:8080/` (start.py:53-58),
which is not a navigable URL.

## Why it matters

A wildcard bind is a reasonable *opt-in* for a LAN receiver, but as the
**default** it maximizes exposure of the surfaces that currently have the
weakest auth. Combined with AUTH-002 (fail-open auth) and PRIV-001 (unauth
admin), the default posture is "reachable by anyone on the network with no
credentials."

## Fix

- Default `--host` to `127.0.0.1`; require an explicit `--host 0.0.0.0` to
  expose on the LAN.
- Consider the same for the collector hub: bind `127.0.0.1` unless a config flag
  opts into `0.0.0.0`, or refuse wildcard binding when no collector keys exist
  (ties into AUTH-002).
- In the banner, print `localhost` (or the machine's LAN IP) instead of the
  literal wildcard `0.0.0.0` for the clickable links.
