# SEC-002 — Live API keys recoverable from git history

- **Severity:** Critical
- **Area:** Security / Credentials
- **Commits:** `ae7c1e6` (added), `24b9593` (removed from working tree)

## What's wrong

`config.json` at commit `ae7c1e6` ("Add API key authentication and admin
dashboard (v3.1)") contains a real `api_keys` block with **3 collector keys and
2 client keys** — actual UUID values, not placeholders. Verified:

```
$ git log --oneline -S api_keys -- config.json
24b9593 Move API keys into secrets config and harden auth/config handling.
ae7c1e6 Add API key authentication and admin dashboard (v3.1)

$ git show ae7c1e6:config.json   # prints all 5 keys in plaintext
```

Commit `24b9593` correctly moved keys out of the working tree into the
gitignored `config.secrets.json` (which was **never** committed — that
mitigation worked). But the blob at `ae7c1e6` is permanent: anyone with the
repo can run one `git show` and recover all five working keys.

## Why it matters

Removing secrets from the current tip does nothing for history. If any of those
same UUIDs were carried forward into the live `config.secrets.json`, the exposed
keys **still authenticate** today: `validate_collector_key` /
`require_api_key` check against whatever is in the secrets file. A collector key
lets an attacker inject spoofed aircraft; a client key grants full API access.

## Fix (do all three — rotation is the only real remedy)

1. **Rotate all 5 keys now.** Generate fresh UUIDs
   (`python -c "import uuid; print(uuid.uuid4())"`), put them in
   `config.secrets.json`, and treat the five committed keys as burned. Update
   every collector and API client with the new values.
2. **Purge the blob from history** — e.g.
   `git filter-repo --path config.json --invert-paths` over the affected range
   (or BFG Repo-Cleaner), then force-push and have all clones re-clone.
3. **If the repo is or ever was shared/public, assume the keys are compromised**
   regardless of cleanup, which is why step 1 is mandatory.
